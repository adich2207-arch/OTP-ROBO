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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
PORT         = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

ptb_app: Application = None

# ── States ────────────────────────────────────────────────────────────────────
(
    DEPOSIT_AMOUNT,
    ADMIN_ADD_SESSION,
    ADMIN_ADD_PRICE,
) = range(3)

# ── Database ──────────────────────────────────────────────────────────────────
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
                id       BIGSERIAL PRIMARY KEY,
                session  TEXT,
                price    NUMERIC(12,2),
                status   TEXT   DEFAULT 'available',
                buyer_id BIGINT DEFAULT NULL
            )
        """)
    logger.info("Database initialised.")

# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_user(user_id: int):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,)
        )

def get_balance(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
        return float(row["balance"]) if row else 0.0

# ── Menu ──────────────────────────────────────────────────────────────────────
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Deposit",     callback_data="menu_deposit")],
        [InlineKeyboardButton("🛒 Buy Account", callback_data="menu_buy")],
        [InlineKeyboardButton("📊 My Balance",  callback_data="menu_balance")],
    ])

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    await update.message.reply_text(
        "🏪 *Welcome to Account Market*\n\n"
        "Buy Telegram accounts instantly.\n"
        "Deposit funds and browse available accounts.",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# ── Balance ───────────────────────────────────────────────────────────────────
async def show_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal = get_balance(query.from_user.id)
    await query.edit_message_text(
        f"📊 *Your Balance*\n\n💵 ${bal:.2f}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
        ])
    )

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏪 *Account Market*\n\nChoose an option:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )


# ── Deposit ───────────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💰 *Deposit*\n\nEnter the amount to deposit (e.g. `50`):\n\n/cancel to go back.",
        parse_mode="Markdown"
    )
    return DEPOSIT_AMOUNT

async def deposit_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive number.")
        return DEPOSIT_AMOUNT

    # Notify admin to verify payment
    await ctx.bot.send_message(
        ADMIN_ID,
        f"📥 *Deposit Request*\n\n"
        f"User: `{update.effective_user.id}` (@{update.effective_user.username or 'N/A'})\n"
        f"Amount: *${amount:.2f}*\n\n"
        f"Use /credit_{update.effective_user.id}_{amount:.2f} to approve.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"✅ Deposit request of *${amount:.2f}* submitted!\n"
        "Admin will verify and credit your balance shortly.",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# Admin credits a user: /credit_<user_id>_<amount>
async def admin_credit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.split("_")
        user_id = int(parts[1])
        amount  = float(parts[2])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /credit_<user_id>_<amount>")
        return

    ensure_user(user_id)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (amount, user_id)
        )

    await update.message.reply_text(f"✅ Credited ${amount:.2f} to user `{user_id}`.", parse_mode="Markdown")
    await ctx.bot.send_message(
        user_id,
        f"🎉 *${amount:.2f} has been added to your balance!*\n\nYou can now buy accounts.",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END

# ── Admin: Add Account ────────────────────────────────────────────────────────
async def add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📋 *Add Account*\n\nPaste the session string for this account:\n\n/cancel to abort.",
        parse_mode="Markdown"
    )
    return ADMIN_ADD_SESSION

async def add_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["session"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ Session saved.\n\nNow enter the price in USD (e.g. `25`):",
        parse_mode="Markdown"
    )
    return ADMIN_ADD_PRICE

async def add_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid price.")
        return ADMIN_ADD_PRICE

    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO accounts (session, price) VALUES (%s, %s) RETURNING id",
            (ctx.user_data["session"], price)
        ).fetchone()

    await update.message.reply_text(
        f"✅ Account #{row['id']} added at *${price:.2f}*\n"
        "It is now visible in the marketplace.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# Admin: list all accounts
async def admin_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT id, price, status, buyer_id FROM accounts ORDER BY id DESC").fetchall()
    if not rows:
        await update.message.reply_text("No accounts yet.")
        return
    icons = {"available": "🟢", "sold": "✅"}
    lines = ["📦 *All Accounts*\n"]
    for r in rows:
        lines.append(f"{icons.get(r['status'],'⚪')} #{r['id']} — ${r['price']:.2f} ({r['status']})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# Admin: delete an account /del_<id>
async def admin_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        acc_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /del_<id>")
        return
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id=%s", (acc_id,))
    await update.message.reply_text(f"🗑 Account #{acc_id} deleted.")


# ── Buy Flow ──────────────────────────────────────────────────────────────────
async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    with get_db() as conn:
        accounts = conn.execute(
            "SELECT id, price FROM accounts WHERE status='available' ORDER BY price ASC"
        ).fetchall()

    if not accounts:
        await query.edit_message_text(
            "😔 *No accounts available right now.*\nCheck back soon!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
            ])
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"🔑 Account #{a['id']}  —  ${a['price']:.2f}",
            callback_data=f"view_{a['id']}"
        )]
        for a in accounts
    ]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])

    await query.edit_message_text(
        f"🛒 *Available Accounts* ({len(accounts)} listed)\n\nSelect one to purchase:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def view_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id  = int(query.data.split("_")[1])
    user_id = query.from_user.id
    ensure_user(user_id)

    with get_db() as conn:
        acc = conn.execute(
            "SELECT id, price FROM accounts WHERE id=%s AND status='available'",
            (acc_id,)
        ).fetchone()

    if not acc:
        await query.edit_message_text(
            "❌ This account is no longer available.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Browse Others", callback_data="menu_buy")]
            ])
        )
        return

    balance   = get_balance(user_id)
    has_funds = balance >= float(acc["price"])

    await query.edit_message_text(
        f"🔑 *Account #{acc['id']}*\n\n"
        f"💵 Price:      *${acc['price']:.2f}*\n"
        f"💼 Balance:    *${balance:.2f}*\n\n"
        f"{'✅ You have enough funds.' if has_funds else '❌ Insufficient balance — deposit first.'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buy Now",  callback_data=f"confirm_{acc_id}")],
            [InlineKeyboardButton("🔙 Back",     callback_data="menu_buy")],
        ])
    )

async def confirm_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    acc_id  = int(query.data.split("_")[1])
    user_id = query.from_user.id
    ensure_user(user_id)

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='available'", (acc_id,)
        ).fetchone()

        if not acc:
            await query.edit_message_text("❌ Account no longer available.")
            return

        balance = get_balance(user_id)
        if balance < float(acc["price"]):
            await query.edit_message_text(
                f"❌ *Insufficient balance.*\n\n"
                f"💼 Your balance: *${balance:.2f}*\n"
                f"💵 Required:     *${acc['price']:.2f}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Deposit", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙 Back",    callback_data="menu_back")],
                ])
            )
            return

        # Deduct balance and mark sold — atomic
        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (acc["price"], user_id)
        )
        conn.execute(
            "UPDATE accounts SET status='sold', buyer_id=%s WHERE id=%s",
            (user_id, acc_id)
        )

    # Confirm to buyer
    await query.edit_message_text(
        f"🎉 *Purchase Successful!*\n\n"
        f"Account #{acc_id} is yours.\n"
        f"Your session string has been sent in a private message.",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

    # Send session string privately
    await ctx.bot.send_message(
        user_id,
        f"🔑 *Your Account Session*\n\n"
        f"Account #{acc_id} — Paid: ${acc['price']:.2f}\n\n"
        f"```\n{acc['session']}\n```\n\n"
        f"Keep this safe. Do not share it with anyone.",
        parse_mode="Markdown"
    )

    # Notify admin
    await ctx.bot.send_message(
        ADMIN_ID,
        f"💸 *Account Sold*\n\n"
        f"Account #{acc_id} sold to user `{user_id}` for ${acc['price']:.2f}.",
        parse_mode="Markdown"
    )

# ── Flask ─────────────────────────────────────────────────────────────────────
@flask_app.get("/")
def health():
    return Response("OK", status=200)

@flask_app.post(f"/webhook/{BOT_TOKEN}")
def webhook():
    import asyncio
    data = request.get_json(force=True)
    upd  = Update.de_json(data, ptb_app.bot)
    asyncio.run(ptb_app.process_update(upd))
    return Response("ok", status=200)

# ── Build & Main ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_start, pattern="^menu_deposit$")],
        states={DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_account", add_account)],
        states={
            ADMIN_ADD_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_session)],
            ADMIN_ADD_PRICE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(deposit_conv)
    app.add_handler(add_conv)

    # Admin commands
    app.add_handler(CommandHandler("accounts", admin_accounts))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/credit_\d+_[\d.]+$") & filters.User(ADMIN_ID), admin_credit
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/del_\d+$") & filters.User(ADMIN_ID), admin_delete
    ))

    # User callbacks
    app.add_handler(CallbackQueryHandler(buy_menu,      pattern="^menu_buy$"))
    app.add_handler(CallbackQueryHandler(view_account,  pattern=r"^view_\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_buy,   pattern=r"^confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(show_balance,  pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(menu_back,     pattern="^menu_back$"))

    return app

def main():
    import asyncio
    global ptb_app
    init_db()
    ptb_app = build_app()

    async def setup():
        await ptb_app.initialize()
        await ptb_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}")
        logger.info("Webhook set.")

    asyncio.run(setup())
    logger.info(f"Starting on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
