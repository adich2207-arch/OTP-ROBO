# bot.py (FINAL WITH OTP LOGIN SYSTEM)

import os
import logging
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

from pyrofork import Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

ptb_app = None

# STATES
(
    DEPOSIT_AMOUNT,
    ADMIN_PHONE,
    ADMIN_OTP,
    ADMIN_ADD_PRICE
) = range(4)

# DB
def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance NUMERIC(12,2) DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id BIGSERIAL PRIMARY KEY,
            session TEXT,
            price NUMERIC(12,2),
            status TEXT DEFAULT 'available',
            buyer_id BIGINT
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id         BIGSERIAL PRIMARY KEY,
            user_id    BIGINT,
            amount     NUMERIC(12,2),
            status     TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)

# HELPERS
def ensure_user(user_id):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,)
        )

def get_balance(user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s",
            (user_id,)
        ).fetchone()
        return float(row["balance"]) if row else 0.0

# START
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "Welcome to Account Market",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
            [InlineKeyboardButton("🛒 Buy Account", callback_data="buy")]
        ])
    )

# DEPOSIT
async def deposit(update: Update, ctx):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "💰 *Deposit USD*\n\nEnter the amount you want to deposit (e.g. `50`):\n\n/cancel to go back.",
        parse_mode="Markdown"
    )
    return DEPOSIT_AMOUNT

async def deposit_amount(update: Update, ctx):
    user = update.effective_user
    ensure_user(user.id)
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive number.")
        return DEPOSIT_AMOUNT

    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s, %s) RETURNING id",
            (user.id, amount)
        ).fetchone()
        dep_id = row["id"]

    # Notify admin
    await ctx.bot.send_message(
        ADMIN_ID,
        f"📥 *New Deposit Request*\n\n"
        f"👤 User: @{user.username or user.first_name} (`{user.id}`)\n"
        f"💵 Amount: *${amount:.2f}*\n"
        f"🆔 Deposit ID: `{dep_id}`\n\n"
        f"✅ Approve: /approve_{dep_id}\n"
        f"❌ Reject:  /reject_{dep_id}\n\n"
        f"Or manually credit: /credit {user.id} {amount:.2f}",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"✅ *Deposit request of ${amount:.2f} submitted!*\n\n"
        f"🆔 Reference ID: `{dep_id}`\n\n"
        f"The admin will verify and credit your balance shortly.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ── Admin: approve/reject deposit ─────────────────────────────────────────────
async def admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve_<id>")
        return

    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await update.message.reply_text("❌ Deposit not found.")
            return
        if dep["status"] != "pending":
            await update.message.reply_text("⚠️ Already processed.")
            return
        conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
        conn.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (dep["amount"], dep["user_id"])
        )

    await update.message.reply_text(
        f"✅ Deposit #{dep_id} approved. *${dep['amount']:.2f}* credited to `{dep['user_id']}`.",
        parse_mode="Markdown"
    )
    await ctx.bot.send_message(
        dep["user_id"],
        f"🎉 *Your deposit of ${dep['amount']:.2f} has been approved!*\n\n"
        f"Your balance has been updated. Start shopping! 🛒",
        parse_mode="Markdown"
    )

async def admin_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reject_<id>")
        return

    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await update.message.reply_text("❌ Not found or already processed.")
            return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))

    await update.message.reply_text(f"❌ Deposit #{dep_id} rejected.")
    await ctx.bot.send_message(
        dep["user_id"],
        f"❌ *Your deposit of ${dep['amount']:.2f} was rejected.*\n\n"
        f"Contact support if you believe this is an error.",
        parse_mode="Markdown"
    )

# ── Admin: manual credit ──────────────────────────────────────────────────────
async def admin_credit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        # Usage: /credit <user_id> <amount>
        parts = update.message.text.strip().split()
        user_id = int(parts[1])
        amount  = float(parts[2])
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/credit <user_id> <amount>`\nExample: `/credit 123456789 50`",
            parse_mode="Markdown"
        )
        return

    ensure_user(user_id)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (amount, user_id)
        )
        new_bal = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()

    await update.message.reply_text(
        f"✅ *${amount:.2f} credited to user `{user_id}`*\n"
        f"New balance: *${float(new_bal['balance']):.2f}*",
        parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(
            user_id,
            f"🎉 *${amount:.2f} has been added to your balance by admin!*\n\n"
            f"Your new balance: *${float(new_bal['balance']):.2f}*",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text("⚠️ Could not notify user (they may not have started the bot).")

# ── Admin: deduct balance ─────────────────────────────────────────────────────
async def admin_deduct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        user_id = int(parts[1])
        amount  = float(parts[2])
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/deduct <user_id> <amount>`\nExample: `/deduct 123456789 10`",
            parse_mode="Markdown"
        )
        return

    with get_db() as conn:
        bal = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
        if not bal or float(bal["balance"]) < amount:
            await update.message.reply_text("❌ User not found or insufficient balance.")
            return
        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (amount, user_id)
        )
        new_bal = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()

    await update.message.reply_text(
        f"✅ *${amount:.2f} deducted from user `{user_id}`*\n"
        f"New balance: *${float(new_bal['balance']):.2f}*",
        parse_mode="Markdown"
    )

# ================= LOGIN SYSTEM =================

async def admin_login(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    await update.message.reply_text("📱 Send phone number (+countrycode):")
    return ADMIN_PHONE

async def get_phone(update: Update, ctx):
    phone = update.message.text.strip()
    ctx.user_data["phone"] = phone

    client = Client("temp", api_id=API_ID, api_hash=API_HASH)
    await client.connect()

    sent = await client.send_code(phone)

    ctx.user_data["client"] = client
    ctx.user_data["phone_code_hash"] = sent.phone_code_hash

    await update.message.reply_text("📩 Enter OTP:")
    return ADMIN_OTP

async def get_otp(update: Update, ctx):
    otp = update.message.text.strip()

    client = ctx.user_data["client"]
    phone = ctx.user_data["phone"]
    code_hash = ctx.user_data["phone_code_hash"]

    try:
        await client.sign_in(phone, code_hash, otp)

        session = await client.export_session_string()
        await client.disconnect()

        ctx.user_data["session"] = session

        await update.message.reply_text("✅ Login successful!\nEnter price:")
        return ADMIN_ADD_PRICE

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def set_price(update: Update, ctx):
    price = float(update.message.text)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO accounts (session, price) VALUES (%s,%s)",
            (ctx.user_data["session"], price)
        )

    await update.message.reply_text("✅ Account added successfully!")
    return ConversationHandler.END

# ================= BUY =================

async def buy_menu(update: Update, ctx):
    query = update.callback_query
    await query.answer()

    with get_db() as conn:
        accounts = conn.execute(
            "SELECT * FROM accounts WHERE status='available'"
        ).fetchall()

    buttons = [
        [InlineKeyboardButton(
            f"Account {a['id']} - ${a['price']}",
            callback_data=f"buy_{a['id']}"
        )]
        for a in accounts
    ]

    await query.message.reply_text("Accounts:", reply_markup=InlineKeyboardMarkup(buttons))

async def buy_account(update: Update, ctx):
    query = update.callback_query
    await query.answer()

    acc_id = int(query.data.split("_")[1])

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='available'",
            (acc_id,)
        ).fetchone()

    balance = get_balance(query.from_user.id)

    await query.message.reply_text(
        f"Price: ${acc['price']}\nBalance: ${balance}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Buy", callback_data=f"confirm_{acc_id}")]
        ])
    )

async def confirm_buy(update: Update, ctx):
    query = update.callback_query
    await query.answer()

    acc_id = int(query.data.split("_")[1])
    user_id = query.from_user.id

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='available'",
            (acc_id,)
        ).fetchone()

        balance = get_balance(user_id)

        if balance < float(acc["price"]):
            await query.message.reply_text("Not enough balance")
            return

        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (acc["price"], user_id)
        )

        conn.execute(
            "UPDATE accounts SET status='sold', buyer_id=%s WHERE id=%s",
            (user_id, acc_id)
        )

    await ctx.bot.send_message(
        user_id,
        f"Session:\n{acc['session']}"
    )

# BUILD
def build_app():
    app = Application.builder().token(BOT_TOKEN).build()

    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit, pattern="deposit")],
        states={DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT, deposit_amount)]},
        fallbacks=[]
    )

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login_account", admin_login)],
        states={
            ADMIN_PHONE: [MessageHandler(filters.TEXT, get_phone)],
            ADMIN_OTP: [MessageHandler(filters.TEXT, get_otp)],
            ADMIN_ADD_PRICE: [MessageHandler(filters.TEXT, set_price)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(deposit_conv)
    app.add_handler(login_conv)

    app.add_handler(CallbackQueryHandler(buy_menu, pattern="buy"))
    app.add_handler(CallbackQueryHandler(buy_account, pattern=r"^buy_\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_buy, pattern=r"^confirm_\d+$"))

    # Admin credit/deduct/approve/reject
    app.add_handler(CommandHandler("credit", admin_credit))
    app.add_handler(CommandHandler("deduct", admin_deduct))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/approve_\d+$") & filters.User(ADMIN_ID), admin_approve
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/reject_\d+$") & filters.User(ADMIN_ID), admin_reject
    ))

    return app

# MAIN
def main():
    global ptb_app
    init_db()
    ptb_app = build_app()

    import asyncio
    async def setup():
        await ptb_app.initialize()
        await ptb_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}")

    asyncio.run(setup())
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
