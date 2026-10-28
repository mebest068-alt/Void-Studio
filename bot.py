import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton
)

TOKEN = "9295443290:AAHWqfAlYeAPHSbPtqQjZdQNz4ggi6PGN8Y"

bot = Bot(token=TOKEN)
dp = Dispatcher()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="👤 Профиль"),
            KeyboardButton(text="🛒 Купить")
        ]
    ],
    resize_keyboard=True
)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Добро пожаловать в магазин 👋",
        reply_markup=keyboard
    )

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = message.from_user
    await message.answer(
        f"👤 Твой профиль\n\n"
        f"Имя: {user.full_name}\n"
        f"ID: {user.id}\n"
        f"Баланс: 0 ₽"
    )

@dp.message(F.text == "🛒 Купить")
async def buy(message: Message):
    await message.answer(
        "🛒 Магазин\n\n"
        "1. Товар №1 — 100 ₽\n"
        "2. Товар №2 — 200 ₽\n\n"
        "Напиши, какой товар хочешь купить."
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
