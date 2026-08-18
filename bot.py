import os
import asyncio
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не найден! "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )

ADMIN_ID = int(os.getenv("ADMIN_ID", "1706479196"))


# =========================================================
# BOT / DISPATCHER
# =========================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# ТИКЕТЫ
# =========================================================

# user_id -> True
tickets = {}


# Админ сейчас отвечает пользователю:
# admin_id -> user_id
reply_mode = {}


# =========================================================
# MUTE
# =========================================================

# user_id -> timestamp окончания мута
#
# Например:
# 123456789 -> 1755555555
#
# Если текущее время меньше timestamp,
# пользователь находится в муте.
muted_users = {}


# =========================================================
# АНТИСПАМ
# =========================================================

# user_id -> список времени последних сообщений
message_times = {}

# Максимальное количество сообщений
# за указанное количество секунд.
ANTI_SPAM_LIMIT = 5
ANTI_SPAM_SECONDS = 10

# Автоматический мут при превышении лимита
AUTO_MUTE_SECONDS = 60


# =========================================================
# ФОРМАТИРОВАНИЕ ВРЕМЕНИ
# =========================================================

def format_duration(seconds: int) -> str:
    """
    Перевод секунд в красивый текст.
    """

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


# =========================================================
# ПРОВЕРКА MUTE
# =========================================================

def is_muted(user_id: int) -> bool:
    """
    Проверяет, находится ли пользователь в муте.
    """

    if user_id not in muted_users:
        return False

    mute_until = muted_users[user_id]

    # Мут закончился
    if time.time() >= mute_until:
        muted_users.pop(user_id, None)
        return False

    return True


def get_mute_remaining(user_id: int) -> int:
    """
    Возвращает оставшееся время мута.
    """

    if user_id not in muted_users:
        return 0

    remaining = int(
        muted_users[user_id] - time.time()
    )

    return max(remaining, 0)


# =========================================================
# КЛАВИАТУРА ГЛАВНОГО МЕНЮ
# =========================================================

def main_menu():
    kb = InlineKeyboardBuilder()

    kb.button(
        text="📩 Создать обращение",
        callback_data="create_ticket"
    )

    return kb.as_markup()


# =========================================================
# КНОПКИ ТИКЕТА
# =========================================================

def ticket_buttons(user_id: int):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="💬 Ответить",
        callback_data=f"reply:{user_id}"
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

    kb.adjust(2, 2)

    return kb.as_markup()


# =========================================================
# КНОПКИ ВЫБОРА МУТА
# =========================================================

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


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в Support!\n\n"
        "Если у тебя возникла проблема, "
        "нажми кнопку ниже 👇",
        reply_markup=main_menu()
    )


# =========================================================
# СОЗДАНИЕ ТИКЕТА
# =========================================================

@dp.callback_query(F.data == "create_ticket")
async def create_ticket(callback: CallbackQuery):
    user_id = callback.from_user.id

    # Если уже есть тикет
    if user_id in tickets:
        await callback.answer(
            "У тебя уже есть открытое обращение.",
            show_alert=True
        )
        return

    tickets[user_id] = True

    await callback.message.answer(
        "📩 Обращение создано!\n\n"
        "Опиши свою проблему одним или несколькими сообщениями."
    )

    await bot.send_message(
        ADMIN_ID,
        "🎫 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"👤 Пользователь: {callback.from_user.full_name}\n"
        f"🆔 ID: {user_id}\n\n"
        "💬 Ожидаю сообщение от пользователя.",
        reply_markup=ticket_buttons(user_id)
    )

    await callback.answer()


# =========================================================
# КНОПКА "ОТВЕТИТЬ"
# =========================================================

@dp.callback_query(F.data.startswith("reply:"))
async def reply_ticket(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )
        return

    user_id = int(
        callback.data.split(":")[1]
    )

    if user_id not in tickets:
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    reply_mode[ADMIN_ID] = user_id

    await callback.message.answer(
        "💬 Режим ответа включён.\n\n"
        f"👤 Пользователь ID: {user_id}\n\n"
        "✏️ Напиши сообщение, которое хочешь отправить пользователю."
    )

    await callback.answer()


# =========================================================
# КНОПКА "MUTE"
# =========================================================

@dp.callback_query(F.data.startswith("mute:"))
async def mute_menu(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )
        return

    user_id = int(
        callback.data.split(":")[1]
    )

    if user_id not in tickets:
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    await callback.message.answer(
        "🔇 Выбери длительность мута:\n\n"
        f"👤 Пользователь: {user_id}",
        reply_markup=mute_duration_keyboard(user_id)
    )

    await callback.answer()


# =========================================================
# ВЫБОР ВРЕМЕНИ MUTE
# =========================================================

@dp.callback_query(F.data.startswith("mutetime:"))
async def set_mute(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )
        return

    parts = callback.data.split(":")

    user_id = int(parts[1])
    duration = int(parts[2])

    if user_id not in tickets:
        await callback.answer(
            "❌ Обращение уже закрыто.",
            show_alert=True
        )
        return

    # =====================================================
    # МУТ НАВСЕГДА
    # =====================================================

    if duration == 0:

        # Очень далёкая дата = фактически навсегда
        muted_users[user_id] = time.time() + (365 * 24 * 60 * 60)

        duration_text = "навсегда"

    else:

        muted_users[user_id] = (
            time.time() + duration
        )

        duration_text = format_duration(duration)

    # =====================================================
    # Уведомляем пользователя
    # =====================================================

    try:

        await bot.send_message(
            user_id,
            "🔇 Вы были временно ограничены в отправке сообщений.\n\n"
            f"⏱ Длительность: {duration_text}\n\n"
            "Пока ограничение действует, ваши сообщения "
            "не будут передаваться в поддержку."
        )

    except Exception:
        pass

    # =====================================================
    # Уведомляем админа
    # =====================================================

    await callback.message.answer(
        "🔇 Пользователь замьючен.\n\n"
        f"👤 ID: {user_id}\n"
        f"⏱ Время: {duration_text}"
    )

    await callback.answer(
        "🔇 Пользователь замьючен."
    )


# =========================================================
# КНОПКА "UNMUTE"
# =========================================================

@dp.callback_query(F.data.startswith("unmute:"))
async def unmute_user(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )
        return

    user_id = int(
        callback.data.split(":")[1]
    )

    if user_id not in muted_users:
        await callback.answer(
            "ℹ️ Пользователь не находится в муте.",
            show_alert=True
        )
        return

    muted_users.pop(
        user_id,
        None
    )

    try:

        await bot.send_message(
            user_id,
            "🔊 Ограничение снято!\n\n"
            "Теперь ты снова можешь отправлять сообщения "
            "в поддержку."
        )

    except Exception:
        pass

    await callback.message.answer(
        "🔊 Пользователь размьючен.\n\n"
        f"👤 ID: {user_id}"
    )

    await callback.answer(
        "🔊 Мут снят."
    )


# =========================================================
# КНОПКА "ЗАКРЫТЬ"
# =========================================================

@dp.callback_query(F.data.startswith("close:"))
async def close_ticket(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "❌ Нет доступа.",
            show_alert=True
        )
        return

    user_id = int(
        callback.data.split(":")[1]
    )

    tickets.pop(
        user_id,
        None
    )

    reply_mode.pop(
        ADMIN_ID,
        None
    )

    muted_users.pop(
        user_id,
        None
    )

    try:

        await bot.send_message(
            user_id,
            "🔒 Ваше обращение было закрыто.\n\n"
            "Если у вас появилась новая проблема, "
            "вы можете создать новое обращение."
        )

    except Exception:
        pass

    await callback.message.answer(
        "✅ Обращение закрыто."
    )

    await callback.answer()


# =========================================================
# СООБЩЕНИЯ ОТ АДМИНА
# =========================================================

@dp.message(F.chat.id == ADMIN_ID)
async def admin_message(message: Message):

    admin_id = message.from_user.id

    if admin_id not in reply_mode:

        await message.answer(
            "📋 Выбери обращение и нажми "
            "«💬 Ответить»."
        )

        return

    user_id = reply_mode[admin_id]

    if user_id not in tickets:

        await message.answer(
            "❌ Это обращение уже закрыто."
        )

        reply_mode.pop(
            admin_id,
            None
        )

        return

    # Если пользователь в муте,
    # админ всё равно может ему отвечать.
    # Mute блокирует только сообщения пользователя.

    if not message.text:

        await message.answer(
            "⚠️ Пока поддерживаются только текстовые сообщения."
        )

        return

    try:

        await bot.send_message(
            user_id,
            "👨‍💼 Ответ поддержки:\n\n"
            f"{message.text}"
        )

        await message.answer(
            "✅ Ответ отправлен пользователю."
        )

    except Exception as e:

        await message.answer(
            f"❌ Не удалось отправить сообщение.\n\n"
            f"{e}"
        )


# =========================================================
# СООБЩЕНИЯ ОТ ПОЛЬЗОВАТЕЛЯ
# =========================================================

@dp.message()
async def user_message(message: Message):

    user_id = message.from_user.id

    # =====================================================
    # АДМИН
    # =====================================================

    if user_id == ADMIN_ID:
        return

    # =====================================================
    # ПРОВЕРКА MUTE
    # =====================================================

    if is_muted(user_id):

        remaining = get_mute_remaining(
            user_id
        )

        await message.answer(
            "🔇 Ты сейчас находишься в муте.\n\n"
            f"⏱ Осталось: {format_duration(remaining)}\n\n"
            "Твои сообщения пока не передаются в поддержку."
        )

        return

    # =====================================================
    # ПРОВЕРКА ТИКЕТА
    # =====================================================

    if user_id not in tickets:

        await message.answer(
            "❗ Сначала создай обращение.",
            reply_markup=main_menu()
        )

        return

    # =====================================================
    # ТОЛЬКО ТЕКСТ
    # =====================================================

    if not message.text:

        await message.answer(
            "⚠️ Пока поддерживаются только текстовые сообщения."
        )

        return

    # =====================================================
    # АНТИСПАМ
    # =====================================================

    current_time = time.time()

    if user_id not in message_times:
        message_times[user_id] = []

    # Оставляем только сообщения за последние N секунд
    message_times[user_id] = [
        msg_time
        for msg_time in message_times[user_id]
        if current_time - msg_time < ANTI_SPAM_SECONDS
    ]

    message_times[user_id].append(
        current_time
    )

    # =====================================================
    # ПРЕВЫШЕН ЛИМИТ
    # =====================================================

    if len(message_times[user_id]) > ANTI_SPAM_LIMIT:

        muted_users[user_id] = (
            current_time + AUTO_MUTE_SECONDS
        )

        # Очищаем историю сообщений
        message_times[user_id] = []

        await message.answer(
            "⚠️ Слишком много сообщений подряд.\n\n"
            f"🔇 Автоматический мут на "
            f"{format_duration(AUTO_MUTE_SECONDS)}.\n\n"
            "Пожалуйста, подожди."
        )

        await bot.send_message(
            ADMIN_ID,
            "🚨 АВТОМАТИЧЕСКИЙ АНТИСПАМ\n\n"
            f"👤 Пользователь: {message.from_user.full_name}\n"
            f"🆔 ID: {user_id}\n\n"
            f"🔇 Мут: {format_duration(AUTO_MUTE_SECONDS)}"
        )

        return

    # =====================================================
    # ОТПРАВЛЯЕМ СООБЩЕНИЕ АДМИНУ
    # =====================================================

    await bot.send_message(
        ADMIN_ID,
        "📨 СООБЩЕНИЕ В ТИКЕТЕ\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"💬 {message.text}",
        reply_markup=ticket_buttons(user_id)
    )

    # =====================================================
    # ПОДТВЕРЖДЕНИЕ
    # =====================================================

    await message.answer(
        "📨 Сообщение отправлено в поддержку.\n"
        "⏳ Ожидай ответа."
    )


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

    # =====================================================
    # WEB SERVER ДЛЯ RENDER
    # =====================================================

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    # Render передаёт PORT
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

    print("================================")
    print("✅ SUPPORT BOT ЗАПУЩЕН")
    print(f"🌐 Port: {port}")
    print(f"👨‍💼 Admin ID: {ADMIN_ID}")
    print("🛡 Anti-Spam: ON")
    print("🔇 Mute: ON")
    print("================================")

    # =====================================================
    # TELEGRAM POLLING
    # =====================================================

    await dp.start_polling(
        bot
    )


# =========================================================
# ЗАПУСК
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
