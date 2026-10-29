import os
import json
import hmac
import hashlib
import logging
import sqlite3
import secrets
from contextlib import contextmanager
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import aiohttp
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION - set these as environment variables on hosting
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9295443290:AAHWqfAlYeAPHSbPtqQjZdQNz4ggi6PGN8Y").strip()
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "").strip()
# Public HTTPS URL of this app, e.g. https://your-app.example.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Comma-separated Telegram numeric user IDs, e.g. 123456789,987654321
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "9119689776").split(",")
    if value.strip().isdigit()
}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
PRICE_CURRENCY = os.getenv("PRICE_CURRENCY", "usd").strip().lower()
PAY_CURRENCY = os.getenv("PAY_CURRENCY", "").strip().lower() or None
DB_PATH = os.getenv("DB_PATH", "shop.sqlite3")
WEB_PORT = int(os.getenv("PORT", "8080"))
NOWPAYMENTS_API_URL = "https://api.nowpayments.io/v1"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_shop")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db_connect():
    """Open a short-lived SQLite connection and always commit/close it."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance TEXT NOT NULL DEFAULT '0.00',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock (
                stock_id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(product_id),
                secret_value TEXT NOT NULL,
                delivered_order_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(product_id),
                product_name TEXT NOT NULL,
                price TEXT NOT NULL,
                price_currency TEXT NOT NULL,
                status TEXT NOT NULL,
                invoice_id TEXT,
                invoice_url TEXT,
                payment_id TEXT,
                payment_status TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_stock_product ON stock(product_id, delivered_order_id);
            """
        )
        # Migrate existing databases created before wallet balance was added.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "balance" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN balance TEXT NOT NULL DEFAULT '0.00'")


def remember_user(update: Update) -> None:
    user = update.effective_user
    if user is None:
        return
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO users(user_id, username, first_name, created_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name""",
            (user.id, user.username, user.first_name, utc_now()),
        )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(value: str) -> str:
    return f"{Decimal(value):.2f} {PRICE_CURRENCY.upper()}"


def get_product(product_id: int):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE product_id=? AND active=1",
            (product_id,),
        ).fetchone()


def stock_count(product_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM stock WHERE product_id=? AND delivered_order_id IS NULL",
            (product_id,),
        ).fetchone()
        return int(row["n"])


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Shop", callback_data="shop")],
        [InlineKeyboardButton("👤 Profile", callback_data="profile"),
         InlineKeyboardButton("📦 My Orders", callback_data="orders")],
        [InlineKeyboardButton("💬 Support", callback_data="support")],
    ])


def nowpayments_signature_is_valid(payload: dict, signature: str) -> bool:
    if not NOWPAYMENTS_IPN_SECRET or not signature:
        return False
    # NOWPayments signs the sorted compact JSON representation using HMAC-SHA512.
    sorted_payload = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )
    expected = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        sorted_payload.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.strip().lower())


async def create_invoice(order_id: str, product_name: str, price: str) -> dict:
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY is not configured")
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must be a public HTTPS URL for IPN callbacks")

    body = {
        "price_amount": str(Decimal(price).quantize(Decimal("0.01"))),
        "price_currency": PRICE_CURRENCY,
        "order_id": order_id,
        "order_description": f"Digital product: {product_name} / order {order_id}",
        "ipn_callback_url": f"{PUBLIC_BASE_URL}/ipn",
        "success_url": "https://t.me/",
        "cancel_url": "https://t.me/",
    }
    if PAY_CURRENCY:
        body["pay_currency"] = PAY_CURRENCY

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{NOWPAYMENTS_API_URL}/invoice",
            headers={"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"},
            json=body,
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                logger.error("NOWPayments invoice error %s: %s", response.status, data)
                raise RuntimeError("Payment provider could not create an invoice. Try again later.")
            if not data.get("invoice_url"):
                logger.error("Unexpected NOWPayments invoice response: %s", data)
                raise RuntimeError("Payment provider returned no invoice URL.")
            return data


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    await update.effective_message.reply_text(
        "🛍️ *Welcome to our Digital Shop!*\n\n"
        "Browse digital products, pay with cryptocurrency, and receive your purchase automatically after the payment is confirmed.",
        reply_markup=home_keyboard(),
        parse_mode="Markdown",
    )


async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    await show_shop_message(update.effective_message.reply_text, update.effective_message)


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if update.effective_user:
        await profile(update, context, update.effective_user.id)


async def myorders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    if update.effective_user:
        await orders(update, context, update.effective_user.id)


async def show_shop_message(_reply_func, message) -> None:
    with db_connect() as conn:
        products = conn.execute(
            "SELECT * FROM products WHERE active=1 ORDER BY product_id"
        ).fetchall()
    if not products:
        await message.reply_text("The shop is empty right now. Please check back later.", reply_markup=home_keyboard())
        return
    rows = []
    for product in products:
        available = stock_count(product["product_id"])
        label = f"{product['name']} — {money(product['price'])} · Stock: {available}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"product:{product['product_id']}")])
    rows.append([InlineKeyboardButton("⬅️ Main menu", callback_data="home")])
    await message.reply_text("🛒 *Shop catalog*\nChoose a product:", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    with db_connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(price AS REAL)), 0) AS total FROM orders WHERE user_id=? AND status='delivered'",
            (user_id,),
        ).fetchone()
    username = f"@{user['username']}" if user and user["username"] else "Not set"
    name = user["first_name"] if user and user["first_name"] else "Customer"
    text = (
        "👤 *Your Profile*\n\n"
        f"Name: {name}\nUsername: {username}\nTelegram ID: `{user_id}`\n"
        f"💰 Balance: {money(user['balance'] if user and user['balance'] else '0.00')}\n"
        f"Completed purchases: {totals['n'] if totals else 0}\n"
        f"Total spent: {Decimal(str(totals['total'] if totals else 0)):.2f} {PRICE_CURRENCY.upper()}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=home_keyboard(), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=home_keyboard(), parse_mode="Markdown")


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT order_id, product_name, price, price_currency, status, created_at FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    if not rows:
        text = "📦 You have no orders yet."
    else:
        lines = ["📦 *Your recent orders*", ""]
        for row in rows:
            lines.append(
                f"• `{row['order_id']}` — {row['product_name']}\n"
                f"  {Decimal(row['price']):.2f} {row['price_currency'].upper()} · *{row['status'].upper()}*"
            )
        text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=home_keyboard(), parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=home_keyboard(), parse_mode="Markdown")


async def buy_product(query, context: ContextTypes.DEFAULT_TYPE, product_id: int) -> None:
    user = query.from_user
    product = get_product(product_id)
    if product is None:
        await query.edit_message_text("This product is no longer available.", reply_markup=home_keyboard())
        return
    if stock_count(product_id) < 1:
        await query.edit_message_text(
            f"❌ *{product['name']}* is currently out of stock.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to shop", callback_data="shop")]]),
            parse_mode="Markdown",
        )
        return

    order_id = secrets.token_hex(8)
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO orders(order_id, user_id, product_id, product_name, price,
               price_currency, status, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, 'creating_invoice', ?, ?)""",
            (order_id, user.id, product_id, product["name"], product["price"], PRICE_CURRENCY, utc_now(), utc_now()),
        )

    await query.edit_message_text("⏳ Creating your secure crypto invoice…")
    try:
        invoice = await create_invoice(order_id, product["name"], product["price"])
    except Exception as exc:
        logger.exception("Could not create invoice")
        with db_connect() as conn:
            conn.execute("UPDATE orders SET status='invoice_error', updated_at=? WHERE order_id=?", (utc_now(), order_id))
        await query.message.reply_text(f"Could not create the invoice. Please try again later.\nOrder reference: `{order_id}`", parse_mode="Markdown", reply_markup=home_keyboard())
        return

    with db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='awaiting_payment', invoice_id=?, invoice_url=?, updated_at=? WHERE order_id=?",
            (str(invoice.get("id", "")), invoice["invoice_url"], utc_now(), order_id),
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Pay with crypto", url=invoice["invoice_url"])],
        [InlineKeyboardButton("🔄 Check order", callback_data=f"check:{order_id}")],
        [InlineKeyboardButton("⬅️ Main menu", callback_data="home")],
    ])
    await query.message.reply_text(
        "🧾 *Order created*\n\n"
        f"Product: {product['name']}\n"
        f"Price: {money(product['price'])}\n"
        f"Order ID: `{order_id}`\n\n"
        "1. Press *Pay with crypto*.\n"
        "2. Complete payment on the provider's page.\n"
        "3. Once the provider confirms the payment, the bot will deliver your digital item here automatically.\n\n"
        "Do not send payment screenshots as proof; only verified provider notifications complete an order.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def buy_with_balance(query, context: ContextTypes.DEFAULT_TYPE, product_id: int) -> None:
    """Atomically charge wallet balance, reserve one stock item, and create a delivered order."""
    user = query.from_user
    order_id = secrets.token_hex(8)
    delivery_value = None
    product_name = None
    insufficient_balance = False
    out_of_stock = False
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        product = conn.execute(
            "SELECT * FROM products WHERE product_id=? AND active=1", (product_id,)
        ).fetchone()
        wallet = conn.execute("SELECT balance FROM users WHERE user_id=?", (user.id,)).fetchone()
        if product is None:
            await query.edit_message_text("This product is no longer available.", reply_markup=home_keyboard())
            return
        if wallet is None or Decimal(wallet["balance"] or "0.00") < Decimal(product["price"]):
            insufficient_balance = True
        else:
            stock_item = conn.execute(
                "SELECT stock_id, secret_value FROM stock WHERE product_id=? AND delivered_order_id IS NULL ORDER BY stock_id LIMIT 1",
                (product_id,),
            ).fetchone()
            if stock_item is None:
                out_of_stock = True
            else:
                new_balance = Decimal(wallet["balance"] or "0.00") - Decimal(product["price"])
                conn.execute("UPDATE users SET balance=? WHERE user_id=?", (f"{new_balance:.2f}", user.id))
                conn.execute(
                    """INSERT INTO orders(order_id, user_id, product_id, product_name, price, price_currency,
                       status, payment_status, delivered_at, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, 'delivered', 'wallet', ?, ?, ?)""",
                    (order_id, user.id, product_id, product["name"], product["price"], PRICE_CURRENCY, utc_now(), utc_now(), utc_now()),
                )
                conn.execute(
                    "UPDATE stock SET delivered_order_id=? WHERE stock_id=? AND delivered_order_id IS NULL",
                    (order_id, stock_item["stock_id"]),
                )
                delivery_value = stock_item["secret_value"]
                product_name = product["name"]
                balance_after = new_balance
    if insufficient_balance:
        await query.answer("Not enough balance. Contact support to top up your balance.", show_alert=True)
        return
    if out_of_stock:
        await query.answer("This product is out of stock.", show_alert=True)
        return
    await query.edit_message_text(
        f"✅ Purchase paid from your balance!\n\nProduct: {product_name}\nOrder: {order_id}\nRemaining balance: {money(f'{balance_after:.2f}')}\n\nYour digital item:\n{delivery_value}",
        reply_markup=home_keyboard(),
    )
    await notify_admins(context.application, f"✅ Wallet order {order_id} completed; user ID: {user.id}; product: {product_name}.")


async def check_order(query, order_id: str) -> None:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None or row["user_id"] != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    status = row["status"]
    await query.answer(f"Order status: {status.replace('_', ' ')}", show_alert=True)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    remember_user(update)
    data = query.data or ""

    if data == "home":
        await query.edit_message_text("🛍️ *Digital Shop*\nChoose an option:", reply_markup=home_keyboard(), parse_mode="Markdown")
    elif data == "shop":
        with db_connect() as conn:
            products = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY product_id").fetchall()
        if not products:
            await query.edit_message_text("The shop is empty right now.", reply_markup=home_keyboard())
            return
        rows = []
        for product in products:
            available = stock_count(product["product_id"])
            rows.append([InlineKeyboardButton(
                f"{product['name']} — {money(product['price'])} · Stock: {available}"[:64],
                callback_data=f"product:{product['product_id']}",
            )])
        rows.append([InlineKeyboardButton("⬅️ Main menu", callback_data="home")])
        await query.edit_message_text("🛒 *Shop catalog*\nChoose a product:", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    elif data.startswith("product:"):
        try:
            product_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        product = get_product(product_id)
        if product is None:
            await query.edit_message_text("This product is unavailable.", reply_markup=home_keyboard())
            return
        available = stock_count(product_id)
        text = (
            f"🛍️ *{product['name']}*\n\n{product['description'] or 'Digital product'}\n\n"
            f"Price: *{money(product['price'])}*\nAvailable: *{available}*"
        )
        keyboard_rows = []
        if available > 0:
            keyboard_rows.append([InlineKeyboardButton("🛒 Buy with crypto", callback_data=f"buy:{product_id}")])
            keyboard_rows.append([InlineKeyboardButton("💰 Buy with balance", callback_data=f"walletbuy:{product_id}")])
        keyboard_rows += [[InlineKeyboardButton("⬅️ Back to shop", callback_data="shop")], [InlineKeyboardButton("🏠 Main menu", callback_data="home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode="Markdown")
    elif data.startswith("buy:"):
        try:
            product_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await buy_product(query, context, product_id)
    elif data.startswith("walletbuy:"):
        try:
            product_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await buy_with_balance(query, context, product_id)
    elif data == "profile":
        await profile(update, context, query.from_user.id)
    elif data == "orders":
        await orders(update, context, query.from_user.id)
    elif data == "support":
        if SUPPORT_USERNAME:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact support", url=f"https://t.me/{SUPPORT_USERNAME}")], [InlineKeyboardButton("⬅️ Main menu", callback_data="home")]])
            await query.edit_message_text("Need help? Contact our support team:", reply_markup=keyboard)
        else:
            await query.edit_message_text("Support is not configured yet. Please contact the shop administrator.", reply_markup=home_keyboard())
    elif data.startswith("check:"):
        await check_order(query, data.split(":", 1)[1])


# ---------------------------- Admin commands ----------------------------
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    # Keep this help text plain: underscores in PRODUCT_ID and TELEGRAM_USER_ID
    # are interpreted as Markdown formatting and can make Telegram reject it.
    await update.effective_message.reply_text(
        "🛠 ADMIN COMMANDS\n\n"
        "/addproduct PRICE | NAME | DESCRIPTION\n"
        "/addstock PRODUCT_ID | ITEM1 || ITEM2 || ITEM3\n"
        "/products - list products and stock\n"
        "/delproduct PRODUCT_ID - hide a product\n"
        "/setprice PRODUCT_ID | PRICE\n"
        "/addbalance USER_ID AMOUNT - add money to a user's wallet\n"
        "/setbalance USER_ID AMOUNT - set a user's wallet balance\n"
        "/balance [USER_ID] - check wallet balance\n"
        "/orders [TELEGRAM_USER_ID] - recent orders\n"
        "/resend ORDER_ID - resend a paid order\n"
        "/adminhelp - show this help\n\n"
        "Examples:\n"
        "/addproduct 9.99 | Game Key | Key for the game\n"
        "/addstock 1 | ABCD-EFGH || IJKL-MNOP"
    )


def parse_amount(raw: str) -> Decimal:
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a valid number, e.g. 10 or 10.50.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return amount


async def addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /addbalance USER_ID AMOUNT\nExample: /addbalance 123456789 10.00")
        return
    try:
        target_id = int(context.args[0])
        amount = parse_amount(context.args[1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO users(user_id, username, first_name, balance, created_at) VALUES(?, NULL, 'Customer', '0.00', ?) ON CONFLICT(user_id) DO NOTHING",
            (target_id, utc_now()),
        )
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (target_id,)).fetchone()
        new_balance = Decimal(row["balance"] or "0.00") + amount
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (f"{new_balance:.2f}", target_id))
    await update.effective_message.reply_text(f"✅ Added {money(f'{amount:.2f}')} to user {target_id}. New balance: {money(f'{new_balance:.2f}')}")
    try:
        await context.bot.send_message(target_id, f"💰 Your shop balance was topped up by {money(f'{amount:.2f}')}.\nCurrent balance: {money(f'{new_balance:.2f}')}")
    except Exception:
        logger.info("Could not notify user %s about wallet top-up", target_id)


async def setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /setbalance USER_ID AMOUNT")
        return
    try:
        target_id = int(context.args[0])
        amount = parse_amount(context.args[1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO users(user_id, username, first_name, balance, created_at) VALUES(?, NULL, 'Customer', '0.00', ?) ON CONFLICT(user_id) DO NOTHING",
            (target_id, utc_now()),
        )
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (f"{amount:.2f}", target_id))
    await update.effective_message.reply_text(f"✅ User {target_id} balance set to {money(f'{amount:.2f}')}")


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    target_id = update.effective_user.id
    if context.args:
        if not is_admin(target_id):
            await update.effective_message.reply_text("⛔ Only admins can check another user's balance.")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /balance [USER_ID]")
            return
    with db_connect() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (target_id,)).fetchone()
    current = row["balance"] if row else "0.00"
    await update.effective_message.reply_text(f"💰 User {target_id} balance: {money(current)}")


def parse_parts(text: str, count: int) -> list[str]:
    parts = [part.strip() for part in text.split("|", count - 1)]
    if len(parts) != count or any(not part for part in parts):
        raise ValueError("Incorrect format. Separate fields with |.")
    return parts


async def addproduct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    try:
        price, name, description = parse_parts(raw, 3)
        amount = Decimal(price)
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
            raise ValueError("Price must be greater than zero and not exceed 1,000,000.")
        price = str(amount.quantize(Decimal("0.01")))
    except (ValueError, InvalidOperation) as exc:
        await update.effective_message.reply_text(f"Usage: /addproduct PRICE | NAME | DESCRIPTION\n{exc}")
        return
    with db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO products(name, description, price, active, created_at) VALUES(?, ?, ?, 1, ?)",
            (name, description, price, utc_now()),
        )
        product_id = cursor.lastrowid
    await update.effective_message.reply_text(f"✅ Product added. ID: {product_id}\n{name} — {money(price)}\nNow add inventory with /addstock.")


async def addstock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    try:
        product_part, items_part = raw.split("|", 1)
        product_id = int(product_part.strip())
        items = [item.strip() for item in items_part.split("||") if item.strip()]
        if not items:
            raise ValueError("Add at least one item after the | separator.")
    except ValueError as exc:
        await update.effective_message.reply_text(f"Usage: /addstock PRODUCT_ID | ITEM1 || ITEM2\n{exc}")
        return
    with db_connect() as conn:
        product = conn.execute("SELECT product_id, name FROM products WHERE product_id=? AND active=1", (product_id,)).fetchone()
        if product is None:
            await update.effective_message.reply_text("Active product not found.")
            return
        conn.executemany(
            "INSERT INTO stock(product_id, secret_value, created_at) VALUES(?, ?, ?)",
            [(product_id, item, utc_now()) for item in items],
        )
    await update.effective_message.reply_text(f"✅ Added {len(items)} stock item(s) to {product['name']} (ID {product_id}).")


async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY product_id").fetchall()
    if not rows:
        await update.effective_message.reply_text("No products yet.")
        return
    lines = ["Products:"]
    for row in rows:
        lines.append(f"ID {row['product_id']} | {row['name']} | {money(row['price'])} | stock {stock_count(row['product_id'])} | {'active' if row['active'] else 'hidden'}")
    await update.effective_message.reply_text("\n".join(lines))


async def delproduct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    try:
        product_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Usage: /delproduct PRODUCT_ID")
        return
    with db_connect() as conn:
        cursor = conn.execute("UPDATE products SET active=0 WHERE product_id=?", (product_id,))
    await update.effective_message.reply_text("✅ Product hidden." if cursor.rowcount else "Product not found.")


async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    try:
        product_part, price_part = parse_parts(raw, 2)
        product_id = int(product_part)
        amount = Decimal(price_part)
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
            raise ValueError("Invalid price.")
        price = str(amount.quantize(Decimal("0.01")))
    except (ValueError, InvalidOperation) as exc:
        await update.effective_message.reply_text(f"Usage: /setprice PRODUCT_ID | PRICE\n{exc}")
        return
    with db_connect() as conn:
        cursor = conn.execute("UPDATE products SET price=? WHERE product_id=? AND active=1", (price, product_id))
    await update.effective_message.reply_text("✅ Price updated." if cursor.rowcount else "Active product not found.")


async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    target_user = None
    if context.args:
        try:
            target_user = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /orders [TELEGRAM_USER_ID]")
            return
    with db_connect() as conn:
        if target_user is None:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (target_user,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("No orders found.")
        return
    lines = ["Recent orders:"]
    for row in rows:
        lines.append(f"{row['order_id']} | user {row['user_id']} | {row['product_name']} | {money(row['price'])} | {row['status']}")
    # Keep messages below Telegram's limit.
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def ipn_handler(request: web.Request) -> web.Response:
    """NOWPayments IPN endpoint. Only signed, finished payments can trigger delivery."""
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    signature = request.headers.get("x-nowpayments-sig", "")
    if not nowpayments_signature_is_valid(payload, signature):
        logger.warning("Rejected IPN with invalid signature")
        return web.Response(status=401, text="Invalid signature")

    order_id = str(payload.get("order_id", ""))
    payment_status = str(payload.get("payment_status", "")).lower()
    payment_id = str(payload.get("payment_id", ""))
    if not order_id:
        return web.Response(status=400, text="Missing order_id")

    # Update payment status, but deliver only when provider says `finished`.
    with db_connect() as conn:
        # Lock before reading/updating so simultaneous retries cannot deliver twice.
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if order is None:
            logger.warning("Signed IPN references unknown order %s", order_id)
            return web.Response(status=200, text="Unknown order ignored")
        conn.execute(
            "UPDATE orders SET payment_id=?, payment_status=?, updated_at=? WHERE order_id=?",
            (payment_id, payment_status, utc_now(), order_id),
        )
        if order["status"] == "delivered":
            return web.Response(status=200, text="Already delivered")
        if payment_status != "finished":
            # Do not deliver for waiting, confirming, confirmed, partially_paid, failed, etc.
            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE order_id=? AND status!='delivered'",
                (f"payment_{payment_status or 'unknown'}", utc_now(), order_id),
            )
            return web.Response(status=200, text="Status recorded; not delivered")

        # Atomic stock claim prevents the same digital item being delivered twice.
        stock_item = conn.execute(
            "SELECT stock_id, secret_value FROM stock WHERE product_id=? AND delivered_order_id IS NULL ORDER BY stock_id LIMIT 1",
            (order["product_id"],),
        ).fetchone()
        if stock_item is None:
            conn.execute("UPDATE orders SET status='paid_out_of_stock', updated_at=? WHERE order_id=?", (utc_now(), order_id))
            return web.Response(status=200, text="Payment received but stock is empty; admin action required")
        conn.execute(
            "UPDATE stock SET delivered_order_id=? WHERE stock_id=? AND delivered_order_id IS NULL",
            (order_id, stock_item["stock_id"]),
        )
        conn.execute(
            "UPDATE orders SET status='delivered', delivered_at=?, updated_at=? WHERE order_id=?",
            (utc_now(), utc_now(), order_id),
        )
        delivery_value = stock_item["secret_value"]
        user_id = int(order["user_id"])
        product_name = order["product_name"]

    # Delivery is sent after the transaction commits. If Telegram fails, admins can resend manually.
    app: Application = request.app["telegram_application"]
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment confirmed — your order is ready!\n\n"
                f"Product: {product_name}\nOrder: {order_id}\n\n"
                "Your digital item:\n"
                f"{delivery_value}\n\n"
                "Keep this information private. Thank you for your purchase!"
            ),
        )
    except Exception:
        logger.exception("Payment completed but Telegram delivery failed for order %s", order_id)
        await notify_admins(app, f"⚠️ Order `{order_id}` is paid and marked delivered, but Telegram delivery failed. Use /resend {order_id}.")
    else:
        await notify_admins(app, f"✅ Order `{order_id}` completed. Product: {product_name}; user ID: {user_id}.")

    return web.Response(status=200, text="OK")


async def notify_admins(app: Application, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception:
            logger.warning("Could not notify admin %s", admin_id)


async def resend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /resend ORDER_ID")
        return
    order_id = context.args[0]
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if row is None or row["status"] not in ("delivered", "paid_out_of_stock"):
            await update.effective_message.reply_text("Order not found or not paid/completed.")
            return
        if row["status"] == "paid_out_of_stock":
            item = conn.execute("SELECT stock_id, secret_value FROM stock WHERE product_id=? AND delivered_order_id IS NULL ORDER BY stock_id LIMIT 1", (row["product_id"],)).fetchone()
            if item is None:
                await update.effective_message.reply_text("No stock available. Add stock first with /addstock.")
                return
            conn.execute("UPDATE stock SET delivered_order_id=? WHERE stock_id=? AND delivered_order_id IS NULL", (order_id, item["stock_id"]))
            conn.execute("UPDATE orders SET status='delivered', delivered_at=?, updated_at=? WHERE order_id=?", (utc_now(), utc_now(), order_id))
            secret_value = item["secret_value"]
        else:
            item = conn.execute("SELECT secret_value FROM stock WHERE delivered_order_id=?", (order_id,)).fetchone()
            if item is None:
                await update.effective_message.reply_text("No delivered inventory item is linked to this order.")
                return
            secret_value = item["secret_value"]
        user_id = row["user_id"]
        product_name = row["product_name"]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📦 Digital delivery for order {order_id}\nProduct: {product_name}\n\n{secret_value}",
        )
        await update.effective_message.reply_text("✅ Delivery message sent.")
    except Exception as exc:
        logger.exception("Manual resend failed")
        await update.effective_message.reply_text(f"Could not message the user: {exc.__class__.__name__}. They may need to start the bot first.")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def post_init(application: Application) -> None:
    init_db()
    if not NOWPAYMENTS_API_KEY:
        logger.warning("NOWPAYMENTS_API_KEY is not set; crypto checkout will not work yet.")
    if not NOWPAYMENTS_IPN_SECRET:
        logger.warning("NOWPAYMENTS_IPN_SECRET is not set; payment notifications will be rejected.")

    app = web.Application(client_max_size=1024 * 1024)
    app["telegram_application"] = application
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/ipn", ipn_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    application.bot_data["web_runner"] = runner
    logger.info("IPN web server listening on port %s", WEB_PORT)


async def post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")
    init_db()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("shop", shop_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("myorders", myorders_command))
    application.add_handler(CommandHandler("adminhelp", admin_help))
    application.add_handler(CommandHandler("addproduct", addproduct))
    application.add_handler(CommandHandler("addstock", addstock))
    application.add_handler(CommandHandler("products", products_command))
    application.add_handler(CommandHandler("delproduct", delproduct))
    application.add_handler(CommandHandler("setprice", setprice))
    application.add_handler(CommandHandler("addbalance", addbalance))
    application.add_handler(CommandHandler("setbalance", setbalance))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("orders", admin_orders))
    application.add_handler(CommandHandler("resend", resend_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Starting Telegram Shop bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
