import os
import logging
import libsql_client
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

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "123456789"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT        = int(os.getenv("PORT", "8080"))
TURSO_URL   = os.getenv("TURSO_URL", "")    # e.g. libsql://your-db.turso.io
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")  # auth token from Turso dashboard

# Global PTB application
ptb_app: Application = None

# Conversation states
(
    DEPOSIT_AMOUNT,
    SELL_USERNAME, SELL_PRICE, SELL_CONFIRM,
    BUY_CONFIRM,
    ADMIN_CONFIRM_DEPOSIT,
) = range(6)

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    """Return a synchronous Turso client."""
    return libsql_client.create_client_sync(
        url=TURSO_URL,
        auth_token=TURSO_TOKEN,
    )

def db_exec(sql: str, args: tuple = ()):
    """Execute a single write statement."""
    with get_db() as db:
        db.execute(sql, list(args))

def db_query(sql: str, args: tuple = ()) -> list[dict]:
    """Execute a read query and return list of dicts."""
    with get_db() as db:
        result = db.execute(sql, list(args))
        cols = result.columns
        return [dict(zip(cols, row)) for row in result.rows]

def db_query_one(sql: str, args: tuple = ()) -> dict | None:
    rows = db_query(sql, args)
    return rows[0] if rows else None

def init_db():
    """Create tables if they don't exist. Safe on every startup."""
    with get_db() as db:
        db.batch([
            """CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                balance    REAL    DEFAULT 0,
                created_at TEXT    DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS deposits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                amount     REAL,
                status     TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id   INTEGER,
                tg_username TEXT,
                price       REAL,
                status      TEXT DEFAULT 'active',
                buyer_id    INTEGER,
                created_at  TEXT DEFAULT (datetime('now'))
            )""",
            """CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id   INTEGER,
                seller_id  INTEGER,
                listing_id INTEGER,
                amount     REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )""",
        ])
    logger.info("Database initialised.")


# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str):
    db_exec(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user_id, username or "")
    )

def get_balance(user_id: int) -> float:
    row = db_query_one("SELECT balance FROM users WHERE user_id=?", (user_id,))
    return float(row["balance"]) if row else 0.0

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Deposit USD",  callback_data="menu_deposit")],
        [InlineKeyboardButton("🛒 Buy Account",  callback_data="menu_buy")],
        [InlineKeyboardButton("📢 Sell Account", callback_data="menu_sell")],
        [InlineKeyboardButton("📊 My Balance",   callback_data="menu_balance")],
        [InlineKeyboardButton("📋 My Listings",  callback_data="menu_mylistings")],
    ])

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    await update.message.reply_text(
        f"👋 Welcome to *TG Market*, {user.first_name}!\n\n"
        "Here you can *buy* and *sell* Telegram accounts using USD.\n"
        "Choose an option below:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

# ── DEPOSIT FLOW ──────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💵 *Deposit USD*\n\n"
        "Send the amount you want to deposit (e.g. `50`).\n"
        "The admin will verify your payment and credit your balance.\n\n"
        "Type /cancel to go back.",
        parse_mode="Markdown"
    )
    return DEPOSIT_AMOUNT

async def deposit_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid positive number.")
        return DEPOSIT_AMOUNT

    with get_db() as db:
        result = db.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (?, ?) RETURNING id",
            [user.id, amount]
        )
        dep_id = result.rows[0][0]

    await ctx.bot.send_message(
        ADMIN_ID,
        f"📥 *New Deposit Request*\n\n"
        f"User: @{user.username or user.first_name} (`{user.id}`)\n"
        f"Amount: *${amount:.2f}*\n"
        f"Deposit ID: `{dep_id}`\n\n"
        f"Use /approve_{dep_id} to confirm.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"✅ Deposit request of *${amount:.2f}* submitted!\n"
        "The admin will verify and credit your balance shortly.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

async def admin_approve_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve_<deposit_id>")
        return

    dep = db_query_one("SELECT * FROM deposits WHERE id=?", (dep_id,))
    if not dep:
        await update.message.reply_text("❌ Deposit not found.")
        return
    if dep["status"] != "pending":
        await update.message.reply_text("⚠️ Already processed.")
        return

    with get_db() as db:
        db.batch([
            ("UPDATE deposits SET status='approved' WHERE id=?", [dep_id]),
            ("UPDATE users SET balance=balance+? WHERE user_id=?", [dep["amount"], dep["user_id"]]),
        ])

    await update.message.reply_text(
        f"✅ Deposit #{dep_id} approved. ${dep['amount']:.2f} credited."
    )
    await ctx.bot.send_message(
        dep["user_id"],
        f"🎉 Your deposit of *${dep['amount']:.2f}* has been approved!\n"
        "Your balance has been updated.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ── SELL FLOW ─────────────────────────────────────────────────────────────────
async def sell_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📢 *List a Telegram Account for Sale*\n\n"
        "Enter the Telegram username you want to sell (without @):\n\n"
        "Type /cancel to go back.",
        parse_mode="Markdown"
    )
    return SELL_USERNAME

async def sell_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uname = update.message.text.strip().lstrip("@")
    if not uname:
        await update.message.reply_text("❌ Invalid username. Try again.")
        return SELL_USERNAME
    ctx.user_data["sell_username"] = uname
    await update.message.reply_text(
        f"✅ Username: *@{uname}*\n\nNow enter your asking price in USD (e.g. `25`):",
        parse_mode="Markdown"
    )
    return SELL_PRICE

async def sell_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive price.")
        return SELL_PRICE

    ctx.user_data["sell_price"] = price
    uname = ctx.user_data["sell_username"]
    await update.message.reply_text(
        f"📋 *Listing Summary*\n\nUsername: *@{uname}*\nPrice: *${price:.2f}*\n\nConfirm?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Listing", callback_data="sell_confirm")],
            [InlineKeyboardButton("❌ Cancel",          callback_data="sell_cancel")],
        ])
    )
    return SELL_CONFIRM

async def sell_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    ensure_user(user.id, user.username)

    if query.data == "sell_cancel":
        await query.edit_message_text("❌ Listing cancelled.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    uname  = ctx.user_data["sell_username"]
    price  = ctx.user_data["sell_price"]
    db_exec(
        "INSERT INTO listings (seller_id, tg_username, price) VALUES (?, ?, ?)",
        (user.id, uname, price)
    )
    await query.edit_message_text(
        f"🎉 *@{uname}* listed for *${price:.2f}*!\nBuyers can now find and purchase it.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

# ── BUY FLOW ──────────────────────────────────────────────────────────────────
async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    listings = db_query(
        "SELECT l.id, l.tg_username, l.price, u.username as seller "
        "FROM listings l JOIN users u ON l.seller_id=u.user_id "
        "WHERE l.status='active' AND l.seller_id != ?",
        (query.from_user.id,)
    )

    if not listings:
        await query.edit_message_text(
            "😔 No accounts available for sale right now.\nCheck back later!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
            ])
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"@{l['tg_username']} — ${l['price']:.2f}",
            callback_data=f"buy_{l['id']}"
        )]
        for l in listings
    ]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
    await query.edit_message_text(
        "🛒 *Available Accounts*\n\nSelect one to purchase:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def buy_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split("_")[1])
    user = query.from_user
    ensure_user(user.id, user.username)

    listing = db_query_one(
        "SELECT l.*, u.username as seller_name FROM listings l "
        "JOIN users u ON l.seller_id=u.user_id WHERE l.id=? AND l.status='active'",
        (listing_id,)
    )
    if not listing:
        await query.edit_message_text("❌ This listing is no longer available.")
        return

    balance = get_balance(user.id)
    has_funds = balance >= float(listing["price"])
    await query.edit_message_text(
        f"🛒 *Purchase Details*\n\n"
        f"Username: *@{listing['tg_username']}*\n"
        f"Price: *${listing['price']:.2f}*\n"
        f"Seller: @{listing['seller_name'] or 'unknown'}\n\n"
        f"Your balance: *${balance:.2f}*\n\n"
        f"{'✅ You have enough funds.' if has_funds else '❌ Insufficient balance. Please deposit first.'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buy Now", callback_data=f"buyconfirm_{listing_id}")],
            [InlineKeyboardButton("❌ Cancel",  callback_data="menu_buy")],
        ])
    )

async def buy_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split("_")[1])
    user = query.from_user
    ensure_user(user.id, user.username)

    listing = db_query_one(
        "SELECT * FROM listings WHERE id=? AND status='active'", (listing_id,)
    )
    if not listing:
        await query.edit_message_text("❌ Listing no longer available.")
        return

    balance = get_balance(user.id)
    if balance < float(listing["price"]):
        await query.edit_message_text(
            f"❌ Insufficient balance.\nYour balance: *${balance:.2f}*\n"
            f"Required: *${listing['price']:.2f}*\n\nPlease deposit first.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return

    # Execute trade atomically
    with get_db() as db:
        db.batch([
            ("UPDATE users SET balance=balance-? WHERE user_id=?",
             [listing["price"], user.id]),
            ("UPDATE users SET balance=balance+? WHERE user_id=?",
             [listing["price"], listing["seller_id"]]),
            ("UPDATE listings SET status='sold', buyer_id=? WHERE id=?",
             [user.id, listing_id]),
            ("INSERT INTO transactions (buyer_id, seller_id, listing_id, amount) VALUES (?,?,?,?)",
             [user.id, listing["seller_id"], listing_id, listing["price"]]),
        ])

    await query.edit_message_text(
        f"🎉 *Purchase Successful!*\n\n"
        f"You bought *@{listing['tg_username']}* for *${listing['price']:.2f}*.\n"
        "The seller has been notified.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    await ctx.bot.send_message(
        listing["seller_id"],
        f"💸 *Your account was sold!*\n\n"
        f"@{listing['tg_username']} sold for *${listing['price']:.2f}*.\n"
        "Your balance has been credited.",
        parse_mode="Markdown"
    )


# ── BALANCE & LISTINGS ────────────────────────────────────────────────────────
async def show_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user.id, user.username)
    balance = get_balance(user.id)
    await query.edit_message_text(
        f"📊 *Your Wallet*\n\nBalance: *${balance:.2f} USD*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Deposit", callback_data="menu_deposit")],
            [InlineKeyboardButton("🔙 Back",    callback_data="menu_back")],
        ])
    )

async def my_listings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    rows = db_query(
        "SELECT * FROM listings WHERE seller_id=? ORDER BY created_at DESC",
        (user.id,)
    )

    if not rows:
        text = "📋 You have no listings yet."
    else:
        icons = {"active": "🟢", "sold": "✅", "cancelled": "🔴"}
        lines = ["📋 *Your Listings*\n"] + [
            f"{icons.get(r['status'], '⚪')} @{r['tg_username']} — ${r['price']:.2f} ({r['status']})"
            for r in rows
        ]
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
        ])
    )

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Choose an option:", reply_markup=main_menu_keyboard())

# ── CANCEL ────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── ADMIN ─────────────────────────────────────────────────────────────────────
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = db_query("SELECT * FROM users ORDER BY created_at DESC")
    lines = ["👥 *All Users*\n"] + [
        f"• @{u['username'] or 'N/A'} (`{u['user_id']}`) — ${u['balance']:.2f}"
        for u in users
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    deps = db_query(
        "SELECT d.*, u.username FROM deposits d JOIN users u ON d.user_id=u.user_id "
        "WHERE d.status='pending'"
    )
    if not deps:
        await update.message.reply_text("✅ No pending deposits.")
        return
    lines = ["📥 *Pending Deposits*\n"] + [
        f"ID `{d['id']}` — @{d['username'] or d['user_id']} — ${d['amount']:.2f}\n  → /approve_{d['id']}"
        for d in deps
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Flask routes ──────────────────────────────────────────────────────────────
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

# ── MAIN ──────────────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_start, pattern="^menu_deposit$")],
        states={DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    sell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sell_start, pattern="^menu_sell$")],
        states={
            SELL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_username)],
            SELL_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price)],
            SELL_CONFIRM:  [CallbackQueryHandler(sell_confirm, pattern="^sell_(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(deposit_conv)
    app.add_handler(sell_conv)
    app.add_handler(CallbackQueryHandler(buy_menu,    pattern="^menu_buy$"))
    app.add_handler(CallbackQueryHandler(buy_item,    pattern=r"^buy_\d+$"))
    app.add_handler(CallbackQueryHandler(buy_confirm, pattern=r"^buyconfirm_\d+$"))
    app.add_handler(CallbackQueryHandler(show_balance, pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(my_listings,  pattern="^menu_mylistings$"))
    app.add_handler(CallbackQueryHandler(menu_back,    pattern="^menu_back$"))
    app.add_handler(CommandHandler("users",   admin_users))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/approve_\d+$") & filters.User(ADMIN_ID),
        admin_approve_deposit
    ))
    return app

def main():
    import asyncio
    global ptb_app
    init_db()
    ptb_app = build_app()

    async def setup_webhook():
        await ptb_app.initialize()
        endpoint = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
        await ptb_app.bot.set_webhook(endpoint)
        logger.info(f"Webhook set: {endpoint}")

    asyncio.run(setup_webhook())
    logger.info(f"Starting on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
