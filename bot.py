import os
import asyncio

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


# =========================
# НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не найден! "
        "Добавь BOT_TOKEN в Environment Variables на Render."
    )

ADMIN_ID = int(os.getenv("ADMIN_ID", "1706479196"))


# =========================
# BOT / DISPATCHER
# =========================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================
# ТИКЕТЫ
# =========================

# user_id -> True
tickets = {}

# admin_id -> user_id
reply_mode = {}


# =========================
# КЛАВИАТУРЫ
# =========================

def main_menu():
    kb = InlineKeyboardBuilder()

    kb.button(
        text="📩 Создать обращение",
        callback_data="create_ticket"
    )

    return kb.as_markup()


def ticket_buttons(user_id: int):
    kb = InlineKeyboardBuilder()

    kb.button(
        text="💬 Ответить",
        callback_data=f"reply:{user_id}"
    )

    kb.button(
        text="🔒 Закрыть",
        callback_data=f"close:{user_id}"
    )

    return kb.as_markup()


# =========================
# /START
# =========================

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в Support!\n\n"
        "Если у тебя возникла проблема, "
        "нажми кнопку ниже 👇",
        reply_markup=main_menu()
    )


# =========================
# СОЗДАНИЕ ТИКЕТА
# =========================

@dp.callback_query(F.data == "create_ticket")
async def create_ticket(callback: CallbackQuery):
    user_id = callback.from_user.id

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
        "💬 Ожидаю сообщение от пользователя."
    )

    await callback.answer()


# =========================
# СООБЩЕНИЯ ОТ АДМИНА
# =========================

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

        del reply_mode[admin_id]
        return

    # Если админ отправил текст
    if message.text:
        await bot.send_message(
            user_id,
            "👨‍💼 Ответ поддержки:\n\n"
            f"{message.text}"
        )

    else:
        await message.answer(
            "⚠️ Пока поддерживаются только текстовые сообщения."
        )
        return

    await message.answer(
        "✅ Ответ отправлен пользователю."
    )


# =========================
# КНОПКА "ОТВЕТИТЬ"
# =========================

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
        f"💬 Режим ответа включён.\n\n"
        f"Пользователь ID: {user_id}\n\n"
        "✏️ Напиши сообщение, которое хочешь отправить пользователю."
    )

    await callback.answer()


# =========================
# КНОПКА "ЗАКРЫТЬ"
# =========================

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

    tickets.pop(user_id, None)
    reply_mode.pop(ADMIN_ID, None)

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


# =========================
# СООБЩЕНИЯ ОТ ПОЛЬЗОВАТЕЛЯ
# =========================

@dp.message()
async def user_message(message: Message):
    user_id = message.from_user.id

    # Админ обрабатывается отдельным handler
    if user_id == ADMIN_ID:
        return

    # Если тикета нет
    if user_id not in tickets:
        await message.answer(
            "❗ Сначала создай обращение.",
            reply_markup=main_menu()
        )
        return

    # Только текст
    if not message.text:
        await message.answer(
            "⚠️ Пока поддерживаются только текстовые сообщения."
        )
        return

    # Отправляем сообщение админу
    await bot.send_message(
        ADMIN_ID,
        "📨 СООБЩЕНИЕ В ТИКЕТЕ\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"💬 {message.text}",
        reply_markup=ticket_buttons(user_id)
    )

    # Подтверждение пользователю
    await message.answer(
        "📨 Сообщение отправлено в поддержку.\n"
        "⏳ Ожидай ответа."
    )


# =========================
# HEALTH CHECK ДЛЯ RENDER
# =========================

async def health(request):
    return web.Response(
        text="Support bot is running!"
    )


# =========================
# MAIN
# =========================

async def main():
    # Создаём веб-приложение для Render
    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    runner = web.AppRunner(app)

    await runner.setup()

    # Render передаёт PORT через Environment
    port = int(
        os.getenv("PORT", "10000")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print("✅ Support bot запущен!")
    print(f"🌐 Port: {port}")
    print(f"👨‍💼 Admin ID: {ADMIN_ID}")

    # Запускаем Telegram polling
    await dp.start_polling(bot)


# =========================
# ЗАПУСК
# =========================

if __name__ == "__main__":
    asyncio.run(main())
