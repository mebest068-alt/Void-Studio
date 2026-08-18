import os
import asyncio

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


BOT_TOKEN = os.getenv("8331988232:AAEP2M_TorpiZy5ucYdUFIuY1rISAAoDAkg")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Открытые обращения:
# user_id -> True
tickets = {}

# Админ сейчас отвечает этому пользователю:
# admin_id -> user_id
reply_mode = {}


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📩 Создать обращение", callback_data="create_ticket")
    return kb.as_markup()


def ticket_buttons(user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Ответить", callback_data=f"reply:{user_id}")
    kb.button(text="🔒 Закрыть", callback_data=f"close:{user_id}")
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в Support!\n\n"
        "Если у тебя возникла проблема, нажми кнопку ниже.",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "create_ticket")
async def create_ticket(callback: CallbackQuery):
    user_id = callback.from_user.id

    if user_id in tickets:
        await callback.answer("У тебя уже есть открытое обращение.", show_alert=True)
        return

    tickets[user_id] = True

    await callback.message.answer(
        "📩 Обращение создано!\n\n"
        "Опиши свою проблему одним или несколькими сообщениями."
    )

    await bot.send_message(
        ADMIN_ID,
        f"🎫 НОВОЕ ОБРАЩЕНИЕ\n\n"
        f"👤 Пользователь: {callback.from_user.full_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"Ожидаю сообщение от пользователя."
    )

    await callback.answer()


@dp.message(F.chat.id == ADMIN_ID)
async def admin_message(message: Message):
    admin_id = message.from_user.id

    if admin_id not in reply_mode:
        await message.answer(
            "Выбери обращение и нажми «💬 Ответить»."
        )
        return

    user_id = reply_mode[admin_id]

    if user_id not in tickets:
        await message.answer("❌ Это обращение уже закрыто.")
        del reply_mode[admin_id]
        return

    await bot.send_message(
        user_id,
        f"👨‍💼 Ответ поддержки:\n\n{message.text}"
    )

    await message.answer("✅ Ответ отправлен пользователю.")


@dp.callback_query(F.data.startswith("reply:"))
async def reply_ticket(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    if user_id not in tickets:
        await callback.answer("Обращение уже закрыто.", show_alert=True)
        return

    reply_mode[ADMIN_ID] = user_id

    await callback.message.answer(
        f"💬 Теперь напиши ответ пользователю ID `{user_id}`."
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("close:"))
async def close_ticket(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    tickets.pop(user_id, None)
    reply_mode.pop(ADMIN_ID, None)

    await bot.send_message(
        user_id,
        "🔒 Ваше обращение было закрыто."
    )

    await callback.message.answer("✅ Обращение закрыто.")
    await callback.answer()


@dp.message()
async def user_message(message: Message):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        return

    if user_id not in tickets:
        await message.answer(
            "Сначала нажми «📩 Создать обращение».",
            reply_markup=main_menu()
        )
        return

    await bot.send_message(
        ADMIN_ID,
        f"📨 СООБЩЕНИЕ В ТИКЕТЕ\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"💬 {message.text}",
        reply_markup=ticket_buttons(user_id)
    )


async def health(request):
    return web.Response(text="Support bot is running!")


async def main():
    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
