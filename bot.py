import os
import asyncio
import time
import sqlite3
import urllib.request
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    raise RuntimeError(
        "BOT_TOKEN не найден! Добавь BOT_TOKEN в Environment Variables."
    )


# =========================================================
# ADMINS
# =========================================================

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")

try:
    ADMIN_IDS = {
        int(x.strip())
        for x in ADMIN_IDS_RAW.split(",")
        if x.strip()
    }
except ValueError:
    raise RuntimeError(
        "ADMIN_IDS/ADMIN_ID должен содержать числовые Telegram ID."
    )

if not ADMIN_IDS:
    raise RuntimeError(
        "Добавь ADMIN_IDS или ADMIN_ID в Environment Variables."
    )


# =========================================================
# DATABASE
# =========================================================

DB_PATH = os.getenv("DB_PATH", "support_bot_v4.db")


# =========================================================
# TIMEZONE / NIGHT MODE
# =========================================================

SUPPORT_TIMEZONE = os.getenv(
    "SUPPORT_TIMEZONE",
    "Europe/Vilnius"
)

try:
    SUPPORT_TZ = ZoneInfo(SUPPORT_TIMEZONE)
except ZoneInfoNotFoundError:
    raise RuntimeError(
        f"Неверный SUPPORT_TIMEZONE: {SUPPORT_TIMEZONE}"
    )


NIGHT_START_HOUR = int(
    os.getenv("NIGHT_START_HOUR", "22")
)

NIGHT_END_HOUR = int(
    os.getenv("NIGHT_END_HOUR", "8")
)


# =========================================================
# ANTI SPAM
# =========================================================

ANTI_SPAM_LIMIT = 5
ANTI_SPAM_SECONDS = 10
AUTO_MUTE_SECONDS = 60


# =========================================================
# CATEGORIES
# =========================================================

CATEGORIES = {
    "payment": "💳 Оплата",
    "bug": "🐛 Ошибка",
    "account": "🔐 Аккаунт",
    "order": "📦 Заказ",
    "other": "❓ Другое",
}


# =========================================================
# PRIORITIES
# =========================================================

PRIORITIES = {
    "low": "🟢 Низкий",
    "normal": "🟡 Обычный",
    "high": "🔴 Высокий",
}


# =========================================================
# ROLES
# =========================================================

ROLES = {
    "owner": "👑 Владелец",
    "admin": "🛡 Администратор",
    "senior": "⭐ Старший оператор",
    "operator": "👨‍💼 Оператор",
    "viewer": "👀 Наблюдатель",
}


# Права доступа.
#
# owner:
#   полный доступ
#
# admin:
#   почти полный доступ, кроме управления owner
#
# senior:
#   тикеты + управление тикетами + статистика
#
# operator:
#   работа с тикетами
#
# viewer:
#   только просмотр
#

ROLE_PERMISSIONS = {
    "owner": {
        "view_tickets",
        "take_ticket",
        "reply",
        "change_category",
        "change_priority",
        "mute",
        "close_ticket",
        "notes",
        "stats",
        "search",
        "broadcast",
        "manage_operators",
        "blacklist",
        "view_history",
    },

    "admin": {
        "view_tickets",
        "take_ticket",
        "reply",
        "change_category",
        "change_priority",
        "mute",
        "close_ticket",
        "notes",
        "stats",
        "search",
        "broadcast",
        "manage_operators",
        "blacklist",
        "view_history",
    },

    "senior": {
        "view_tickets",
        "take_ticket",
        "reply",
        "change_category",
        "change_priority",
        "mute",
        "close_ticket",
        "notes",
        "stats",
        "search",
        "view_history",
    },

    "operator": {
        "view_tickets",
        "take_ticket",
        "reply",
        "change_category",
        "change_priority",
        "notes",
        "view_history",
    },

    "viewer": {
        "view_tickets",
        "view_history",
    },
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

# admin_id -> broadcast mode
broadcast_mode: set[int] = set()


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    return conn


def init_db():
    with closing(db_connect()) as db:

        # USERS
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL
            )
        """)

        # OPERATORS
        db.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'operator',
                added_at REAL NOT NULL
            )
        """)

        # Tickets
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

        # Messages
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

        # Notes
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

        # Ratings
        db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                ticket_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
            )
        """)

        # Mutes
        db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                user_id INTEGER PRIMARY KEY,
                mute_until REAL
            )
        """)

        # Blacklist
        db.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at REAL NOT NULL
            )
        """)

        # Stats
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
                """
                INSERT OR IGNORE INTO stats(key, value)
                VALUES(?, 0)
                """,
                (key,)
            )

        # -------------------------------------------------
        # MIGRATION:
        # Если база была от старой версии без role,
        # добавляем колонку.
        # -------------------------------------------------

        columns = db.execute(
            "PRAGMA table_info(operators)"
        ).fetchall()

        column_names = {
            row["name"]
            for row in columns
        }

        if "role" not in column_names:
            db.execute(
                """
                ALTER TABLE operators
                ADD COLUMN role TEXT NOT NULL DEFAULT 'operator'
                """
            )

        # -------------------------------------------------
        # ADMIN IDS автоматически становятся owner.
        # -------------------------------------------------

        now = time.time()

        for admin_id in ADMIN_IDS:

            db.execute(
                """
                INSERT OR IGNORE INTO operators(
                    user_id,
                    role,
                    added_at
                )
                VALUES(?, 'owner', ?)
                """,
                (
                    admin_id,
                    now
                )
            )

            # Если существовал как operator,
            # но находится в ADMIN_IDS — делаем owner.
            db.execute(
                """
                UPDATE operators
                SET role = 'owner'
                WHERE user_id = ?
                """,
                (admin_id,)
            )

        db.commit()


# =========================================================
# STATS
# =========================================================

def stat_inc(
    key: str,
    amount: int = 1
):
    with closing(db_connect()) as db:

        db.execute(
            """
            UPDATE stats
            SET value = value + ?
            WHERE key = ?
            """,
            (
                amount,
                key
            )
        )

        db.commit()


def stat_get(key: str) -> int:
    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT value
            FROM stats
            WHERE key = ?
            """,
            (key,)
        ).fetchone()

    return int(row["value"]) if row else 0


# =========================================================
# USERS
# =========================================================

def upsert_user(
    user_id: int,
    full_name: str,
    username: str | None
):
    now = time.time()

    with closing(db_connect()) as db:

        db.execute("""
            INSERT INTO users(
                user_id,
                full_name,
                username,
                created_at,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                full_name = excluded.full_name,
                username = excluded.username,
                last_seen = excluded.last_seen
        """, (
            user_id,
            full_name,
            username,
            now,
            now
        ))

        db.commit()


def get_user(user_id: int):

    with closing(db_connect()) as db:

        return db.execute(
            """
            SELECT *
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()


# =========================================================
# ROLES / PERMISSIONS
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_role(user_id: int) -> str | None:

    if user_id in ADMIN_IDS:
        return "owner"

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT role
            FROM operators
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

    if not row:
        return None

    return row["role"]


def is_operator(user_id: int) -> bool:
    return get_role(user_id) is not None


def has_permission(
    user_id: int,
    permission: str
) -> bool:

    role = get_role(user_id)

    if not role:
        return False

    permissions = ROLE_PERMISSIONS.get(
        role,
        set()
    )

    return permission in permissions


def require_permission(
    user_id: int,
    permission: str
) -> bool:

    return has_permission(
        user_id,
        permission
    )


def add_operator(
    user_id: int,
    role: str = "operator"
):

    if role not in ROLES:
        role = "operator"

    with closing(db_connect()) as db:

        db.execute(
            """
            INSERT OR REPLACE INTO operators(
                user_id,
                role,
                added_at
            )
            VALUES(?, ?, ?)
            """,
            (
                user_id,
                role,
                time.time()
            )
        )

        db.commit()


def remove_operator(user_id: int):

    if is_admin(user_id):
        return

    with closing(db_connect()) as db:

        db.execute(
            """
            DELETE FROM operators
            WHERE user_id = ?
            """,
            (user_id,)
        )

        db.commit()


def set_operator_role(
    user_id: int,
    role: str
):

    if role not in ROLES:
        return False

    if is_admin(user_id):
        return False

    with closing(db_connect()) as db:

        db.execute(
            """
            UPDATE operators
            SET role = ?
            WHERE user_id = ?
            """,
            (
                role,
                user_id
            )
        )

        db.commit()

    return True


def get_operators():

    with closing(db_connect()) as db:

        return db.execute(
            """
            SELECT *
            FROM operators
            ORDER BY added_at ASC
            """
        ).fetchall()


# =========================================================
# TICKETS
# =========================================================

def get_open_ticket(user_id: int):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE
                t.user_id = ?
                AND t.status = 'open'
            ORDER BY
                t.ticket_id DESC
            LIMIT 1
        """, (
            user_id,
        )).fetchone()


def get_ticket(ticket_id: int):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE t.ticket_id = ?
        """, (
            ticket_id,
        )).fetchone()


def get_open_tickets(
    limit: int = 50
):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE t.status = 'open'
            ORDER BY
                CASE t.priority
                    WHEN 'high' THEN 0
                    WHEN 'normal' THEN 1
                    ELSE 2
                END,
                t.created_at ASC
            LIMIT ?
        """, (
            limit,
        )).fetchall()


def get_my_tickets(
    operator_id: int,
    limit: int = 50
):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE
                t.status = 'open'
                AND t.operator_id = ?
            ORDER BY
                t.updated_at DESC
            LIMIT ?
        """, (
            operator_id,
            limit
        )).fetchall()


def create_ticket(
    user_id: int,
    category: str = "other",
    priority: str = "normal"
) -> int:

    now = time.time()

    with closing(db_connect()) as db:

        cur = db.execute("""
            INSERT INTO tickets(
                user_id,
                category,
                priority,
                status,
                operator_id,
                created_at,
                updated_at,
                closed_at
            )
            VALUES(
                ?,
                ?,
                ?,
                'open',
                NULL,
                ?,
                ?,
                NULL
            )
        """, (
            user_id,
            category,
            priority,
            now,
            now
        ))

        ticket_id = cur.lastrowid

        db.commit()

    stat_inc("tickets_created")

    return int(ticket_id)


def close_ticket(ticket_id: int):

    now = time.time()

    with closing(db_connect()) as db:

        db.execute("""
            UPDATE tickets
            SET
                status = 'closed',
                updated_at = ?,
                closed_at = ?
            WHERE
                ticket_id = ?
                AND status = 'open'
        """, (
            now,
            now,
            ticket_id
        ))

        db.commit()

    stat_inc("tickets_closed")


def assign_ticket(
    ticket_id: int,
    operator_id: int
):

    with closing(db_connect()) as db:

        db.execute("""
            UPDATE tickets
            SET
                operator_id = ?,
                updated_at = ?
            WHERE
                ticket_id = ?
                AND status = 'open'
        """, (
            operator_id,
            time.time(),
            ticket_id
        ))

        db.commit()


def update_ticket_category(
    ticket_id: int,
    category: str
):

    with closing(db_connect()) as db:

        db.execute("""
            UPDATE tickets
            SET
                category = ?,
                updated_at = ?
            WHERE ticket_id = ?
        """, (
            category,
            time.time(),
            ticket_id
        ))

        db.commit()


def update_ticket_priority(
    ticket_id: int,
    priority: str
):

    with closing(db_connect()) as db:

        db.execute("""
            UPDATE tickets
            SET
                priority = ?,
                updated_at = ?
            WHERE ticket_id = ?
        """, (
            priority,
            time.time(),
            ticket_id
        ))

        db.commit()


def search_tickets(
    query: str,
    limit: int = 20
):

    query = query.strip()

    with closing(db_connect()) as db:

        if query.isdigit():

            return db.execute("""
                SELECT
                    t.*,
                    u.full_name,
                    u.username
                FROM tickets t
                JOIN users u
                    ON u.user_id = t.user_id
                WHERE
                    t.ticket_id = ?
                    OR t.user_id = ?
                ORDER BY
                    t.created_at DESC
                LIMIT ?
            """, (
                int(query),
                int(query),
                limit
            )).fetchall()

        like = f"%{query}%"

        return db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE
                u.full_name LIKE ?
                OR COALESCE(u.username, '') LIKE ?
            ORDER BY
                t.created_at DESC
            LIMIT ?
        """, (
            like,
            like,
            limit
        )).fetchall()


def count_open_tickets():

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM tickets
            WHERE status = 'open'
            """
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
    message_type_value: str
):

    with closing(db_connect()) as db:

        db.execute("""
            INSERT INTO messages(
                ticket_id,
                sender_id,
                sender_type,
                message_id,
                message_type,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
        """, (
            ticket_id,
            sender_id,
            sender_type,
            message_id,
            message_type_value,
            time.time()
        ))

        db.execute(
            """
            UPDATE tickets
            SET updated_at = ?
            WHERE ticket_id = ?
            """,
            (
                time.time(),
                ticket_id
            )
        )

        db.commit()


def save_note(
    ticket_id: int,
    operator_id: int,
    text: str
):

    with closing(db_connect()) as db:

        db.execute("""
            INSERT INTO notes(
                ticket_id,
                operator_id,
                text,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?
            )
        """, (
            ticket_id,
            operator_id,
            text,
            time.time()
        ))

        db.commit()


def get_ticket_notes(ticket_id: int):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT *
            FROM notes
            WHERE ticket_id = ?
            ORDER BY created_at DESC
            LIMIT 10
        """, (
            ticket_id,
        )).fetchall()


def get_ticket_messages(ticket_id: int):

    with closing(db_connect()) as db:

        return db.execute("""
            SELECT *
            FROM messages
            WHERE ticket_id = ?
            ORDER BY created_at ASC
            LIMIT 100
        """, (
            ticket_id,
        )).fetchall()


def set_rating(
    ticket_id: int,
    user_id: int,
    rating: int
):

    with closing(db_connect()) as db:

        db.execute("""
            INSERT OR REPLACE INTO ratings(
                ticket_id,
                user_id,
                rating,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?
            )
        """, (
            ticket_id,
            user_id,
            rating,
            time.time()
        ))

        db.commit()

    stat_inc("ratings_received")


def get_rating(ticket_id: int):

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT rating
            FROM ratings
            WHERE ticket_id = ?
            """,
            (ticket_id,)
        ).fetchone()

    return int(row["rating"]) if row else None


def average_rating():

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT
                AVG(rating) AS avg_rating,
                COUNT(*) AS c
            FROM ratings
            """
        ).fetchone()

    return (
        float(row["avg_rating"] or 0),
        int(row["c"] or 0)
    )


# =========================================================
# MUTE / BLACKLIST
# =========================================================

def set_mute(
    user_id: int,
    duration: int
):

    # duration == 0 => permanent

    mute_until = (
        None
        if duration == 0
        else time.time() + duration
    )

    with closing(db_connect()) as db:

        db.execute("""
            INSERT OR REPLACE INTO mutes(
                user_id,
                mute_until
            )
            VALUES(
                ?,
                ?
            )
        """, (
            user_id,
            mute_until
        ))

        db.commit()


def remove_mute(user_id: int):

    with closing(db_connect()) as db:

        db.execute(
            """
            DELETE FROM mutes
            WHERE user_id = ?
            """,
            (user_id,)
        )

        db.commit()


def is_muted(user_id: int) -> bool:

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT mute_until
            FROM mutes
            WHERE user_id = ?
            """,
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
            """
            SELECT mute_until
            FROM mutes
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

    if not row:
        return 0

    if row["mute_until"] is None:
        return -1

    return max(
        0,
        int(
            float(row["mute_until"])
            - time.time()
        )
    )


def blacklist_user(
    user_id: int,
    reason: str = ""
):

    with closing(db_connect()) as db:

        db.execute("""
            INSERT OR REPLACE INTO blacklist(
                user_id,
                reason,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?
            )
        """, (
            user_id,
            reason,
            time.time()
        ))

        db.commit()


def unblacklist_user(user_id: int):

    with closing(db_connect()) as db:

        db.execute(
            """
            DELETE FROM blacklist
            WHERE user_id = ?
            """,
            (user_id,)
        )

        db.commit()


def is_blacklisted(user_id: int) -> bool:

    with closing(db_connect()) as db:

        row = db.execute(
            """
            SELECT 1
            FROM blacklist
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

    return row is not None


# =========================================================
# TIME HELPERS
# =========================================================

def local_now():

    return time.datetime.now(
        SUPPORT_TZ
    )


def is_night_time() -> bool:

    # Используем timestamp, чтобы не зависеть
    # от времени сервера.
    from datetime import datetime

    now = datetime.now(SUPPORT_TZ)

    hour = now.hour

    # 22:00 -> 23:59
    if hour >= NIGHT_START_HOUR:
        return True

    # 00:00 -> 07:59
    if hour < NIGHT_END_HOUR:
        return True

    return False


def night_message() -> str:

    return (
        "🌙 <b>Сейчас поддержка работает "
        "в ограниченном режиме.</b>\n\n"
        "Ответим утром."
    )


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

    from datetime import datetime

    dt = datetime.fromtimestamp(
        timestamp,
        SUPPORT_TZ
    )

    return dt.strftime(
        "%d.%m.%Y %H:%M"
    )


def category_name(value: str) -> str:

    return CATEGORIES.get(
        value,
        "❓ Другое"
    )


def priority_name(value: str) -> str:

    return PRIORITIES.get(
        value,
        "🟡 Обычный"
    )


def role_name(value: str | None) -> str:

    if not value:
        return "Не назначена"

    return ROLES.get(
        value,
        value
    )


def ticket_label(ticket) -> str:

    username = (
        f"@{ticket['username']}"
        if ticket["username"]
        else "нет username"
    )

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

    kb.button(
        text="📩 Создать обращение",
        callback_data="create_ticket"
    )

    kb.button(
        text="🎫 Мой тикет",
        callback_data="my_ticket"
    )

    kb.adjust(1)

    return kb.as_markup()


def category_keyboard():

    kb = InlineKeyboardBuilder()

    for key, name in CATEGORIES.items():

        kb.button(
            text=name,
            callback_data=f"category:{key}"
        )

    kb.adjust(1)

    return kb.as_markup()


def priority_keyboard(prefix: str = "priority"):

    kb = InlineKeyboardBuilder()

    for key, name in PRIORITIES.items():

        kb.button(
            text=name,
            callback_data=f"{prefix}:{key}"
        )

    kb.adjust(1)

    return kb.as_markup()


def ticket_buttons(ticket_id: int):

    kb = InlineKeyboardBuilder()

    kb.button(
        text="💬 Ответить",
        callback_data=f"reply:{ticket_id}"
    )

    kb.button(
        text="👨‍💼 Взять",
        callback_data=f"take:{ticket_id}"
    )

    kb.button(
        text="📂 Категория",
        callback_data=f"catmenu:{ticket_id}"
    )

    kb.button(
        text="🚦 Приоритет",
        callback_data=f"priomenu:{ticket_id}"
    )

    kb.button(
        text="👤 Информация",
        callback_data=f"info:{ticket_id}"
    )

    kb.button(
        text="📜 История",
        callback_data=f"history:{ticket_id}"
    )

    kb.button(
        text="📝 Заметка",
        callback_data=f"note:{ticket_id}"
    )

    kb.button(
        text="🔇 Mute",
        callback_data=f"mute:{ticket_id}"
    )

    kb.button(
        text="🔊 Unmute",
        callback_data=f"unmute:{ticket_id}"
    )

    kb.button(
        text="🔒 Закрыть",
        callback_data=f"close:{ticket_id}"
    )

    kb.adjust(
        2,
        2,
        2,
        2,
        2,
        1
    )

    return kb.as_markup()


def operator_panel():

    kb = InlineKeyboardBuilder()

    kb.button(
        text="📥 Новые обращения",
        callback_data="admin_open"
    )

    kb.button(
        text="👨‍💼 Мои тикеты",
        callback_data="admin_my"
    )

    kb.button(
        text="🔴 Срочные",
        callback_data="admin_high"
    )

    kb.button(
        text="📊 Статистика",
        callback_data="admin_stats"
    )

    kb.button(
        text="👥 Операторы",
        callback_data="admin_operators"
    )

    kb.button(
        text="📢 Рассылка",
        callback_data="admin_broadcast"
    )

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

    kb.adjust(
        2,
        2,
        1
    )

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


def role_keyboard(user_id: int):

    kb = InlineKeyboardBuilder()

    for role, name in ROLES.items():

        if role == "owner":
            continue

        kb.button(
            text=name,
            callback_data=f"setrole:{user_id}:{role}"
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

    role = get_role(user_id)

    if role:

        await message.answer(
            "👨‍💼 <b>Support CRM</b>\n\n"
            f"Твоя роль: {role_name(role)}\n\n"
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

@dp.callback_query(
    F.data == "create_ticket"
)
async def create_ticket_start(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    if is_blacklisted(user_id):

        await callback.answer(
            "🚫 Доступ к поддержке ограничен.",
            show_alert=True
        )

        return

    if is_muted(user_id):

        remaining = get_mute_remaining(
            user_id
        )

        text = (
            "🔇 Постоянный мут."
            if remaining == -1
            else
            f"🔇 Ты находишься в муте.\n"
            f"⏱ Осталось: {format_duration(remaining)}"
        )

        await callback.answer(
            text,
            show_alert=True
        )

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


@dp.callback_query(
    F.data.startswith("category:")
)
async def create_ticket_category(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    category = callback.data.split(
        ":",
        1
    )[1]

    if category not in CATEGORIES:

        await callback.answer(
            "❌ Неверная категория.",
            show_alert=True
        )

        return

    if get_open_ticket(user_id):

        await callback.answer(
            "У тебя уже есть открытый тикет.",
            show_alert=True
        )

        return

    ticket_id = create_ticket(
        user_id,
        category,
        "normal"
    )

    ticket = get_ticket(ticket_id)

    await callback.message.answer(
        f"📩 <b>Обращение #{ticket_id} создано!</b>\n\n"
        f"📂 Категория: {category_name(category)}\n"
        f"🚦 Приоритет: {priority_name('normal')}\n\n"
        "Опиши проблему одним или несколькими сообщениями.\n"
        "После этого дождись ответа поддержки."
    )

    # -----------------------------------------------------
    # NIGHT MODE
    # -----------------------------------------------------

    if is_night_time():

        await callback.message.answer(
            night_message()
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
        f"🚦 {priority_name('normal')}\n"
    )

    if is_night_time():

        admin_text += (
            "\n🌙 <b>НОЧНОЙ РЕЖИМ</b>\n"
            "Обращение создано с 22:00 до 08:00."
        )

    else:

        admin_text += (
            "\n⏳ Ожидаю сообщение от пользователя."
        )

    for admin_id in ADMIN_IDS:

        try:

            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=ticket_buttons(
                    ticket_id
                )
            )

        except Exception:
            pass

    await callback.answer(
        "✅ Тикет создан!"
    )


# =========================================================
# USER: MY TICKET
# =========================================================

@dp.callback_query(
    F.data == "my_ticket"
)
async def my_ticket(
    callback: CallbackQuery
):

    ticket = get_open_ticket(
        callback.from_user.id
    )

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
        "Ты можешь продолжать отправлять сообщения "
        "в этот тикет."
    )

    await callback.answer()


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(Command("admin"))
async def admin_command(
    message: Message
):

    if not has_permission(
        message.from_user.id,
        "view_tickets"
    ):
        return

    role = get_role(
        message.from_user.id
    )

    await message.answer(
        "👨‍💼 <b>Панель Support CRM</b>\n\n"
        f"Твоя роль: {role_name(role)}",
        reply_markup=operator_panel()
    )


# =========================================================
# TICKETS
# =========================================================

@dp.message(Command("tickets"))
async def admin_tickets(
    message: Message
):

    if not require_permission(
        message.from_user.id,
        "view_tickets"
    ):
        return

    tickets = get_open_tickets()

    if not tickets:

        await message.answer(
            "📭 Открытых обращений нет."
        )

        return

    await message.answer(
        f"📥 <b>Открытые обращения: {len(tickets)}</b>",
        reply_markup=ticket_list_keyboard(
            tickets
        )
    )


def ticket_list_keyboard(
    tickets
):

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
            f"{ticket['full_name'][:25]}"
            f"{username[:15]}"
        )

        kb.button(
            text=text,
            callback_data=(
                f"showticket:{ticket['ticket_id']}"
            )
        )

    kb.adjust(1)

    return kb.as_markup()


# =========================================================
# ADMIN OPEN
# =========================================================

@dp.callback_query(
    F.data == "admin_open"
)
async def admin_open(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_tickets"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    tickets = get_open_tickets()

    if not tickets:

        await callback.message.answer(
            "📭 Открытых обращений нет."
        )

    else:

        await callback.message.answer(
            f"📥 <b>Открытые обращения: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(
                tickets
            )
        )

    await callback.answer()


# =========================================================
# MY TICKETS
# =========================================================

@dp.callback_query(
    F.data == "admin_my"
)
async def admin_my(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_tickets"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    tickets = get_my_tickets(
        callback.from_user.id
    )

    if not tickets:

        await callback.message.answer(
            "📭 На тебе нет открытых тикетов."
        )

    else:

        await callback.message.answer(
            f"👨‍💼 <b>Мои тикеты: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(
                tickets
            )
        )

    await callback.answer()


# =========================================================
# HIGH PRIORITY
# =========================================================

@dp.callback_query(
    F.data == "admin_high"
)
async def admin_high(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_tickets"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    with closing(db_connect()) as db:

        tickets = db.execute("""
            SELECT
                t.*,
                u.full_name,
                u.username
            FROM tickets t
            JOIN users u
                ON u.user_id = t.user_id
            WHERE
                t.status = 'open'
                AND t.priority = 'high'
            ORDER BY
                t.created_at ASC
            LIMIT 50
        """).fetchall()

    if not tickets:

        await callback.message.answer(
            "🟢 Срочных тикетов нет."
        )

    else:

        await callback.message.answer(
            f"🔴 <b>Срочные обращения: {len(tickets)}</b>",
            reply_markup=ticket_list_keyboard(
                tickets
            )
        )

    await callback.answer()


# =========================================================
# SHOW TICKET
# =========================================================

@dp.callback_query(
    F.data.startswith("showticket:")
)
async def show_ticket(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_tickets"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

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
        reply_markup=ticket_buttons(
            ticket_id
        )
    )

    await callback.answer()


# =========================================================
# TAKE
# =========================================================

@dp.callback_query(
    F.data.startswith("take:")
)
async def take_ticket(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "take_ticket"
    ):

        await callback.answer(
            "❌ Твоя роль не позволяет брать тикеты.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":

        await callback.answer(
            "❌ Тикет закрыт.",
            show_alert=True
        )

        return

    assign_ticket(
        ticket_id,
        callback.from_user.id
    )

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
        f"✅ Тикет #{ticket_id} назначен на "
        f"оператора <code>{callback.from_user.id}</code>."
    )

    await callback.answer(
        "✅ Тикет взят."
    )


# =========================================================
# REPLY
# =========================================================

@dp.callback_query(
    F.data.startswith("reply:")
)
async def reply_ticket(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "reply"
    ):

        await callback.answer(
            "❌ Твоя роль не позволяет отвечать.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":

        await callback.answer(
            "❌ Тикет закрыт.",
            show_alert=True
        )

        return

    assign_ticket(
        ticket_id,
        callback.from_user.id
    )

    reply_mode[
        callback.from_user.id
    ] = ticket_id

    await callback.message.answer(
        "💬 <b>РЕЖИМ ОТВЕТА ВКЛЮЧЁН</b>\n\n"
        f"🎫 Тикет: #{ticket_id}\n"
        f"👤 Пользователь: {ticket['full_name']}\n\n"
        "Теперь отправь текст, фото, видео, документ "
        "или другое поддерживаемое сообщение.\n\n"
        "Для выхода: /cancel"
    )

    await callback.answer()


# =========================================================
# CANCEL
# =========================================================

@dp.message(Command("cancel"))
async def admin_cancel(
    message: Message
):

    if not is_operator(
        message.from_user.id
    ):
        return

    reply_mode.pop(
        message.from_user.id,
        None
    )

    broadcast_mode.discard(
        message.from_user.id
    )

    await message.answer(
        "❌ Режим отключён."
    )


# =========================================================
# USER INFO
# =========================================================

@dp.callback_query(
    F.data.startswith("info:")
)
async def user_info(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_tickets"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    mute = "нет"

    if is_muted(ticket["user_id"]):

        remaining = get_mute_remaining(
            ticket["user_id"]
        )

        mute = (
            "навсегда"
            if remaining == -1
            else format_duration(
                remaining
            )
        )

    blacklist = (
        "🚫 Да"
        if is_blacklisted(ticket["user_id"])
        else "Нет"
    )

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
# HISTORY
# =========================================================

@dp.callback_query(
    F.data.startswith("history:")
)
async def ticket_history(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "view_history"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    messages = get_ticket_messages(
        ticket_id
    )

    notes = get_ticket_notes(
        ticket_id
    )

    lines = [
        f"📜 <b>История тикета #{ticket_id}</b>\n"
    ]

    if messages:

        lines.append(
            f"💬 Сообщений: {len(messages)}\n"
        )

        for item in messages[-30:]:

            sender = (
                "👤 Пользователь"
                if item["sender_type"] == "user"
                else "👨‍💼 Оператор"
            )

            lines.append(
                f"{sender} — "
                f"{format_dt(item['created_at'])} — "
                f"{item['message_type']}"
            )

    else:

        lines.append(
            "📭 Сообщений нет."
        )

    if notes:

        lines.append(
            "\n📝 <b>Заметки:</b>"
        )

        for note in notes:

            lines.append(
                f"\n{format_dt(note['created_at'])}\n"
                f"{note['text']}"
            )

    await callback.message.answer(
        "\n".join(lines)
    )

    await callback.answer()


# =========================================================
# CATEGORY
# =========================================================

@dp.callback_query(
    F.data.startswith("catmenu:")
)
async def category_menu(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "change_category"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    await callback.message.answer(
        f"📂 Выбери категорию для "
        f"тикета #{ticket_id}:",
        reply_markup=category_ticket_keyboard(
            ticket_id
        )
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("setcat:")
)
async def set_category(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "change_category"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    _, ticket_id, category = (
        callback.data.split(":")
    )

    ticket_id = int(ticket_id)

    if category not in CATEGORIES:

        await callback.answer(
            "❌ Неверная категория.",
            show_alert=True
        )

        return

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    update_ticket_category(
        ticket_id,
        category
    )

    await callback.message.answer(
        f"✅ Категория тикета #{ticket_id} изменена:\n"
        f"{category_name(category)}"
    )

    await callback.answer()


# =========================================================
# PRIORITY
# =========================================================

@dp.callback_query(
    F.data.startswith("priomenu:")
)
async def priority_menu(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "change_priority"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    await callback.message.answer(
        f"🚦 Выбери приоритет для "
        f"тикета #{ticket_id}:",
        reply_markup=priority_ticket_keyboard(
            ticket_id
        )
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("setprio:")
)
async def set_priority(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "change_priority"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    _, ticket_id, priority = (
        callback.data.split(":")
    )

    ticket_id = int(ticket_id)

    if priority not in PRIORITIES:

        await callback.answer(
            "❌ Неверный приоритет.",
            show_alert=True
        )

        return

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    update_ticket_priority(
        ticket_id,
        priority
    )

    await callback.message.answer(
        f"✅ Приоритет тикета #{ticket_id} изменён:\n"
        f"{priority_name(priority)}"
    )

    await callback.answer()


# =========================================================
# NOTES
# =========================================================

@dp.callback_query(
    F.data.startswith("note:")
)
async def note_start(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "notes"
    ):

        await callback.answer(
            "❌ Твоя роль не позволяет создавать заметки.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    reply_mode[
        callback.from_user.id
    ] = -ticket_id

    await callback.message.answer(
        f"📝 <b>Внутренняя заметка "
        f"для тикета #{ticket_id}</b>\n\n"
        "Отправь текст заметки. "
        "Пользователь её не увидит.\n\n"
        "Для отмены: /cancel"
    )

    await callback.answer()


# =========================================================
# MUTE
# =========================================================

@dp.callback_query(
    F.data.startswith("mute:")
)
async def mute_menu(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "mute"
    ):

        await callback.answer(
            "❌ Твоя роль не позволяет мутить.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    await callback.message.answer(
        f"🔇 Выбери длительность мута "
        f"для пользователя из тикета #{ticket_id}:",
        reply_markup=mute_duration_keyboard(
            ticket_id
        )
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("mutetime:")
)
async def set_mute_callback(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "mute"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    _, ticket_id, duration = (
        callback.data.split(":")
    )

    ticket_id = int(ticket_id)
    duration = int(duration)

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    user_id = ticket["user_id"]

    set_mute(
        user_id,
        duration
    )

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
            "Пока ограничение действует, "
            "сообщения в поддержку "
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


# =========================================================
# UNMUTE
# =========================================================

@dp.callback_query(
    F.data.startswith("unmute:")
)
async def unmute_user(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "mute"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

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
            "Теперь ты снова можешь отправлять "
            "сообщения в поддержку."
        )

    except Exception:
        pass

    await callback.message.answer(
        f"🔊 Мут снят.\n"
        f"👤 ID: {user_id}"
    )

    await callback.answer(
        "🔊 Мут снят."
    )


# =========================================================
# CLOSE
# =========================================================

@dp.callback_query(
    F.data.startswith("close:")
)
async def close_ticket_callback(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "close_ticket"
    ):

        await callback.answer(
            "❌ Твоя роль не позволяет закрывать тикеты.",
            show_alert=True
        )

        return

    ticket_id = int(
        callback.data.split(":")[1]
    )

    ticket = get_ticket(ticket_id)

    if not ticket or ticket["status"] != "open":

        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )

        return

    close_ticket(ticket_id)

    remove_mute(
        ticket["user_id"]
    )

    for operator_id, active_ticket_id in list(
        reply_mode.items()
    ):

        if active_ticket_id == ticket_id:

            reply_mode.pop(
                operator_id,
                None
            )

    try:

        await bot.send_message(
            ticket["user_id"],
            f"🔒 <b>ОБРАЩЕНИЕ #{ticket_id} ЗАКРЫТО</b>\n\n"
            "Спасибо за обращение!\n\n"
            "Пожалуйста, оцени работу поддержки:",
            reply_markup=rating_keyboard(
                ticket_id
            )
        )

    except Exception:
        pass

    await callback.message.answer(
        f"✅ Тикет #{ticket_id} закрыт."
    )

    await callback.answer(
        "🔒 Тикет закрыт."
    )


# =========================================================
# RATING
# =========================================================

@dp.callback_query(
    F.data.startswith("rate:")
)
async def rate_ticket(
    callback: CallbackQuery
):

    _, ticket_id, rating = (
        callback.data.split(":")
    )

    ticket_id = int(ticket_id)
    rating = int(rating)

    ticket = get_ticket(ticket_id)

    if not ticket:

        await callback.answer(
            "❌ Тикет не найден.",
            show_alert=True
        )

        return

    if ticket["user_id"] != callback.from_user.id:

        await callback.answer(
            "❌ Это не твой тикет.",
            show_alert=True
        )

        return

    if not 1 <= rating <= 5:

        await callback.answer(
            "❌ Неверная оценка.",
            show_alert=True
        )

        return

    if get_rating(ticket_id) is not None:

        await callback.answer(
            "⭐ Оценка уже оставлена.",
            show_alert=True
        )

        return

    set_rating(
        ticket_id,
        callback.from_user.id,
        rating
    )

    await callback.message.answer(
        f"❤️ Спасибо! Ты поставил оценку: "
        f"{'⭐' * rating}"
    )

    await callback.answer(
        "Спасибо за оценку!"
    )


# =========================================================
# STATS
# =========================================================

@dp.message(Command("stats"))
async def admin_stats_command(
    message: Message
):

    if not require_permission(
        message.from_user.id,
        "stats"
    ):
        return

    avg, ratings_count = average_rating()

    role = get_role(
        message.from_user.id
    )

    await message.answer(
        "📊 <b>СТАТИСТИКА SUPPORT CRM</b>\n\n"
        f"👤 Твоя роль: {role_name(role)}\n\n"
        f"🎫 Создано тикетов: "
        f"{stat_get('tickets_created')}\n"
        f"🔒 Закрыто тикетов: "
        f"{stat_get('tickets_closed')}\n"
        f"📂 Открыто сейчас: "
        f"{count_open_tickets()}\n\n"
        f"📨 Получено сообщений: "
        f"{stat_get('messages_received')}\n"
        f"📤 Отправлено сообщений: "
        f"{stat_get('messages_sent')}\n"
        f"🚨 Автомутов: "
        f"{stat_get('auto_mutes')}\n\n"
        f"⭐ Средняя оценка: "
        f"{avg:.2f}/5\n"
        f"⭐ Оценок получено: "
        f"{ratings_count}"
    )


@dp.callback_query(
    F.data == "admin_stats"
)
async def admin_stats_callback(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "stats"
    ):

        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )

        return

    avg, ratings_count = average_rating()

    await callback.message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"🎫 Создано: "
        f"{stat_get('tickets_created')}\n"
        f"🔒 Закрыто: "
        f"{stat_get('tickets_closed')}\n"
        f"📂 Открыто: "
        f"{count_open_tickets()}\n\n"
        f"📨 Получено сообщений: "
        f"{stat_get('messages_received')}\n"
        f"📤 Отправлено сообщений: "
        f"{stat_get('messages_sent')}\n"
        f"🚨 Автомутов: "
        f"{stat_get('auto_mutes')}\n\n"
        f"⭐ Средняя оценка: "
        f"{avg:.2f}/5\n"
        f"⭐ Оценок: "
        f"{ratings_count}"
    )

    await callback.answer()


# =========================================================
# OPERATORS
# =========================================================

@dp.callback_query(
    F.data == "admin_operators"
)
async def admin_operators(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "manage_operators"
    ):

        await callback.answer(
            "❌ Только администратор.",
            show_alert=True
        )

        return

    operators = get_operators()

    lines = [
        "👥 <b>ОПЕРАТОРЫ</b>\n"
    ]

    for op in operators:

        role = role_name(
            op["role"]
        )

        lines.append(
            f"{role}: "
            f"<code>{op['user_id']}</code>"
        )

    lines.append(
        "\n<b>Добавить:</b>\n"
        "<code>/addoperator ID роль</code>\n\n"
        "Пример:\n"
        "<code>/addoperator 123456789 operator</code>\n\n"
        "<b>Роли:</b>\n"
        "owner — владелец\n"
        "admin — администратор\n"
        "senior — старший оператор\n"
        "operator — оператор\n"
        "viewer — наблюдатель\n\n"
        "<b>Удалить:</b>\n"
        "<code>/deloperator ID</code>"
    )

    await callback.message.answer(
        "\n".join(lines)
    )

    await callback.answer()


# =========================================================
# ADD OPERATOR
# =========================================================

@dp.message(Command("addoperator"))
async def add_operator_command(
    message: Message
):

    if not has_permission(
        message.from_user.id,
        "manage_operators"
    ):
        return

    parts = message.text.split()

    if len(parts) < 2:

        await message.answer(
            "Использование:\n"
            "<code>/addoperator 123456789 operator</code>\n\n"
            "Роли:\n"
            "owner\n"
            "admin\n"
            "senior\n"
            "operator\n"
            "viewer"
        )

        return

    if not parts[1].isdigit():

        await message.answer(
            "❌ Telegram ID должен быть числом."
        )

        return

    user_id = int(parts[1])

    role = (
        parts[2].lower()
        if len(parts) >= 3
        else "operator"
    )

    if role == "owner":

        await message.answer(
            "❌ Роль owner нельзя назначить через команду."
        )

        return

    if role not in ROLES:

        await message.answer(
            "❌ Неизвестная роль.\n\n"
            "Доступно:\n"
            "admin\n"
            "senior\n"
            "operator\n"
            "viewer"
        )

        return

    add_operator(
        user_id,
        role
    )

    await message.answer(
        "✅ Пользователь добавлен.\n\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"👤 Роль: {role_name(role)}"
    )


# =========================================================
# DELETE OPERATOR
# =========================================================

@dp.message(Command("deloperator"))
async def del_operator_command(
    message: Message
):

    if not has_permission(
        message.from_user.id,
        "manage_operators"
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    if (
        len(parts) != 2
        or not parts[1].isdigit()
    ):

        await message.answer(
            "Использование: "
            "<code>/deloperator 123456789</code>"
        )

        return

    operator_id = int(
        parts[1]
    )

    if is_admin(operator_id):

        await message.answer(
            "❌ Главного администратора "
            "удалить нельзя."
        )

        return

    remove_operator(
        operator_id
    )

    await message.answer(
        f"✅ Оператор <code>{operator_id}</code> удалён."
    )


# =========================================================
# CHANGE ROLE
# =========================================================

@dp.message(Command("setrole"))
async def setrole_command(
    message: Message
):

    if not has_permission(
        message.from_user.id,
        "manage_operators"
    ):
        return

    parts = message.text.split()

    if len(parts) != 3:

        await message.answer(
            "Использование:\n"
            "<code>/setrole ID роль</code>\n\n"
            "Пример:\n"
            "<code>/setrole 123456789 senior</code>"
        )

        return

    if not parts[1].isdigit():

        await message.answer(
            "❌ ID должен быть числом."
        )

        return

    user_id = int(parts[1])
    role = parts[2].lower()

    if user_id in ADMIN_IDS:

        await message.answer(
            "❌ Роль owner для главного "
            "администратора менять нельзя."
        )

        return

    if role == "owner":

        await message.answer(
            "❌ Owner назначается только через ADMIN_IDS."
        )

        return

    if role not in ROLES:

        await message.answer(
            "❌ Неизвестная роль."
        )

        return

    if get_role(user_id) is None:

        await message.answer(
            "❌ Этот пользователь не является оператором."
        )

        return

    set_operator_role(
        user_id,
        role
    )

    await message.answer(
        f"✅ Роль пользователя "
        f"<code>{user_id}</code> изменена:\n\n"
        f"{role_name(role)}"
    )


# =========================================================
# SEARCH
# =========================================================

@dp.message(Command("search"))
async def search_command(
    message: Message
):

    if not require_permission(
        message.from_user.id,
        "search"
    ):
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "🔎 Использование:\n"
            "<code>/search username</code>\n"
            "<code>/search имя</code>\n"
            "<code>/search ticket_id</code>"
        )

        return

    results = search_tickets(
        parts[1]
    )

    if not results:

        await message.answer(
            "🔎 Ничего не найдено."
        )

        return

    await message.answer(
        f"🔎 Найдено: {len(results)}",
        reply_markup=ticket_list_keyboard(
            results
        )
    )


# =========================================================
# BROADCAST
# =========================================================

@dp.callback_query(
    F.data == "admin_broadcast"
)
async def broadcast_start(
    callback: CallbackQuery
):

    if not require_permission(
        callback.from_user.id,
        "broadcast"
    ):

        await callback.answer(
            "❌ Только администратор.",
            show_alert=True
        )

        return

    broadcast_mode.add(
        callback.from_user.id
    )

    await callback.message.answer(
        "📢 <b>РЕЖИМ РАССЫЛКИ</b>\n\n"
        "Отправь сообщение, которое нужно "
        "разослать пользователям.\n\n"
        "Можно отправить текст, фото, видео, "
        "документ и т.д.\n\n"
        "Для отмены: /cancel"
    )

    await callback.answer()


# =========================================================
# ADMIN MESSAGES
# =========================================================

@dp.message(
    F.chat.id.in_(ADMIN_IDS)
)
async def admin_message(
    message: Message
):

    admin_id = message.from_user.id

    # Команды обрабатываются отдельными handlers.
    if (
        message.text
        and message.text.startswith("/")
    ):
        return

    # -----------------------------------------------------
    # BROADCAST
    # -----------------------------------------------------

    if admin_id in broadcast_mode:

        broadcast_mode.discard(
            admin_id
        )

        with closing(db_connect()) as db:

            users = db.execute(
                "SELECT user_id FROM users"
            ).fetchall()

        sent = 0
        failed = 0

        for row in users:

            user_id = int(
                row["user_id"]
            )

            try:

                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=admin_id,
                    message_id=message.message_id
                )

                sent += 1

                await asyncio.sleep(
                    0.04
                )

            except Exception:

                failed += 1

        await message.answer(
            "📢 <b>Рассылка завершена</b>\n\n"
            f"✅ Отправлено: {sent}\n"
            f"❌ Ошибок: {failed}"
        )

        return

    # -----------------------------------------------------
    # OPERATOR CHECK
    # -----------------------------------------------------

    if not is_operator(admin_id):
        return

    # -----------------------------------------------------
    # NOTE MODE
    # -----------------------------------------------------

    if (
        admin_id in reply_mode
        and reply_mode[admin_id] < 0
    ):

        if not require_permission(
            admin_id,
            "notes"
        ):

            reply_mode.pop(
                admin_id,
                None
            )

            await message.answer(
                "❌ Нет доступа к заметкам."
            )

            return

        ticket_id = abs(
            reply_mode[admin_id]
        )

        ticket = get_ticket(
            ticket_id
        )

        if not ticket:

            reply_mode.pop(
                admin_id,
                None
            )

            await message.answer(
                "❌ Тикет не найден."
            )

            return

        if not message.text:

            await message.answer(
                "❌ Заметка должна быть текстом."
            )

            return

        save_note(
            ticket_id,
            admin_id,
            message.text
        )

        reply_mode.pop(
            admin_id,
            None
        )

        await message.answer(
            f"📝 Заметка для тикета "
            f"#{ticket_id} сохранена."
        )

        return

    # -----------------------------------------------------
    # REPLY MODE
    # -----------------------------------------------------

    if admin_id not in reply_mode:

        await message.answer(
            "📋 Выбери обращение через "
            "/tickets или нажми "
            "«💬 Ответить»."
        )

        return

    if not require_permission(
        admin_id,
        "reply"
    ):

        reply_mode.pop(
            admin_id,
            None
        )

        await message.answer(
            "❌ Твоя роль не позволяет "
            "отвечать пользователям."
        )

        return

    ticket_id = reply_mode[
        admin_id
    ]

    ticket = get_ticket(
        ticket_id
    )

    if (
        not ticket
        or ticket["status"] != "open"
    ):

        await message.answer(
            "❌ Это обращение уже закрыто."
        )

        reply_mode.pop(
            admin_id,
            None
        )

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

        stat_inc(
            "messages_sent"
        )

        await message.answer(
            f"✅ Сообщение отправлено "
            f"в тикет #{ticket_id}."
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
async def user_message(
    message: Message
):

    user_id = message.from_user.id

    # Операторы обрабатываются отдельным handler.
    if is_operator(user_id):
        return

    upsert_user(
        user_id,
        message.from_user.full_name,
        message.from_user.username
    )

    stat_inc(
        "messages_received"
    )

    if is_blacklisted(user_id):

        await message.answer(
            "🚫 Доступ к поддержке ограничен."
        )

        return

    # -----------------------------------------------------
    # MUTE
    # -----------------------------------------------------

    if is_muted(user_id):

        remaining = get_mute_remaining(
            user_id
        )

        if remaining == -1:

            await message.answer(
                "🔇 <b>Ты находишься "
                "в постоянном муте.</b>\n\n"
                "Твои сообщения не передаются "
                "в поддержку."
            )

        else:

            await message.answer(
                "🔇 <b>Ты сейчас находишься "
                "в муте.</b>\n\n"
                f"⏱ Осталось: "
                f"{format_duration(remaining)}"
            )

        return

    # -----------------------------------------------------
    # TICKET
    # -----------------------------------------------------

    ticket = get_open_ticket(
        user_id
    )

    if not ticket:

        await message.answer(
            "❗ Сначала создай обращение.",
            reply_markup=user_main_menu()
        )

        return

    # -----------------------------------------------------
    # ANTI SPAM
    # -----------------------------------------------------

    now = time.time()

    history = message_times.setdefault(
        user_id,
        []
    )

    history[:] = [
        t
        for t in history
        if now - t < ANTI_SPAM_SECONDS
    ]

    history.append(now)

    if len(history) > ANTI_SPAM_LIMIT:

        history.clear()

        set_mute(
            user_id,
            AUTO_MUTE_SECONDS
        )

        stat_inc(
            "auto_mutes"
        )

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
                    f"🔇 Мут: "
                    f"{format_duration(AUTO_MUTE_SECONDS)}",
                    reply_markup=ticket_buttons(
                        ticket["ticket_id"]
                    )
                )

            except Exception:
                pass

        return

    # -----------------------------------------------------
    # SAVE MESSAGE
    # -----------------------------------------------------

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

    if is_night_time():

        header += (
            "\n\n🌙 <b>НОЧНОЙ РЕЖИМ</b>"
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
                reply_markup=ticket_buttons(
                    ticket["ticket_id"]
                )
            )

        except Exception:
            pass

    await message.answer(
        f"📨 Сообщение отправлено в поддержку.\n"
        f"🎫 Тикет #{ticket['ticket_id']}\n"
        "⏳ Ожидай ответа."
    )


# =========================================================
# HEALTH CHECK
# =========================================================

async def health(
    request
):

    return web.Response(
        text="Support Bot V4 is running!"
    )


# =========================================================
# KEEP ALIVE
# =========================================================

KEEP_ALIVE_INTERVAL = int(
    os.getenv(
        "KEEP_ALIVE_INTERVAL",
        "600"
    )
)


async def keep_alive():

    base_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not base_url:

        print(
            "ℹ️ RENDER_EXTERNAL_URL не найден — "
            "Keep-Alive отключён."
        )

        return

    health_url = (
        base_url.rstrip("/")
        + "/health"
    )

    print(
        f"🟢 Render Keep-Alive включён: "
        f"каждые {KEEP_ALIVE_INTERVAL} сек."
    )

    while True:

        try:

            await asyncio.sleep(
                KEEP_ALIVE_INTERVAL
            )

            def ping():

                request = urllib.request.Request(
                    health_url,
                    headers={
                        "User-Agent":
                        "SupportBot-Render-KeepAlive/1.0"
                    }
                )

                with urllib.request.urlopen(
                    request,
                    timeout=10
                ) as response:

                    return response.status

            status = await asyncio.to_thread(
                ping
            )

            print(
                f"💓 Keep-Alive: HTTP {status}"
            )

        except asyncio.CancelledError:

            print(
                "🛑 Render Keep-Alive остановлен."
            )

            raise

        except Exception as e:

            print(
                f"⚠️ Keep-Alive ошибка: {e}"
            )


# =========================================================
# MAIN
# =========================================================

async def main():

    init_db()

    await bot.delete_webhook(
        drop_pending_updates=False
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    keep_alive_task = asyncio.create_task(
        keep_alive()
    )

    print("================================")
    print("✅ SUPPORT BOT V4 ЗАПУЩЕН")
    print(f"🌐 Port: {port}")
    print(f"👑 Owners: {sorted(ADMIN_IDS)}")
    print(f"💾 SQLite: {DB_PATH}")
    print(f"🌍 Timezone: {SUPPORT_TIMEZONE}")
    print(
        f"🌙 Night Mode: "
        f"{NIGHT_START_HOUR}:00 - "
        f"{NIGHT_END_HOUR}:00"
    )
    print("🛡 Anti-Spam: ON")
    print("🔇 Mute: ON")
    print("🎫 Tickets: ON")
    print("👨‍💼 Operators: ON")
    print("👥 Roles: ON")
    print("📂 Categories: ON")
    print("🚦 Priorities: ON")
    print("⭐ Ratings: ON")
    print("📝 Notes: ON")
    print("📜 History: ON")
    print("📢 Broadcast: ON")
    print("================================")

    try:

        await dp.start_polling(
            bot
        )

    except Exception as e:

        print(
            "❌ Polling остановлен."
        )

        print(
            f"Причина: {e}"
        )

        print(
            "Проверь, что этот BOT_TOKEN "
            "не используется другим "
            "запущенным ботом/сервисом."
        )

        raise

    finally:

        keep_alive_task.cancel()

        try:

            await keep_alive_task

        except asyncio.CancelledError:

            pass

        await runner.cleanup()

        await bot.session.close()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
