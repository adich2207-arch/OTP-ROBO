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

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "123456789"))
SUPPORT_USER = os.getenv("SUPPORT_USERNAME", "YourSupportUsername")  # without @
WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "")
PORT         = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

REFERRAL_COMMISSION = 0.02   # 2%

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
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      BIGINT PRIMARY KEY,
                username     TEXT,
                balance      NUMERIC(12,2) DEFAULT 0,
                referred_by  BIGINT        DEFAULT NULL,
                created_at   TIMESTAMPTZ   DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT,
                amount     NUMERIC(12,2),
                status     TEXT        DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id          BIGSERIAL PRIMARY KEY,
                seller_id   BIGINT,
                tg_username TEXT,
                price       NUMERIC(12,2),
                status      TEXT        DEFAULT 'active',
                buyer_id    BIGINT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         BIGSERIAL PRIMARY KEY,
                buyer_id   BIGINT,
                seller_id  BIGINT,
                listing_id BIGINT,
                amount     NUMERIC(12,2),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id          BIGSERIAL PRIMARY KEY,
                referrer_id BIGINT,
                referred_id BIGINT,
                deposit_id  BIGINT,
                commission  NUMERIC(12,2),
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Add referred_by column if upgrading from old schema
        conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL
        """)
    logger.info("✅ Database initialised.")


# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str, referred_by: int = None):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (user_id, username, referred_by) VALUES (%s, %s, %s)",
                (user_id, username or "", referred_by)
            )

def get_balance(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
        return float(row["balance"]) if row else 0.0

def get_referral_count(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE referred_by=%s", (user_id,)
        ).fetchone()
        return row["cnt"] if row else 0

def get_referral_earnings(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(commission),0) AS total FROM referral_earnings WHERE referrer_id=%s",
            (user_id,)
        ).fetchone()
        return float(row["total"]) if row else 0.0

def get_bot_username(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.bot.username or "this_bot"

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Deposit",   callback_data="menu_deposit"),
            InlineKeyboardButton("💸 Withdraw",  callback_data="menu_withdraw"),
        ],
        [
            InlineKeyboardButton("🛒 Buy Account",  callback_data="menu_buy"),
            InlineKeyboardButton("📢 Sell Account", callback_data="menu_sell"),
        ],
        [
            InlineKeyboardButton("📊 My Wallet",    callback_data="menu_balance"),
            InlineKeyboardButton("📋 My Listings",  callback_data="menu_mylistings"),
        ],
        [
            InlineKeyboardButton("👥 Refer & Earn", callback_data="menu_refer"),
            InlineKeyboardButton("🆘 Support",      url=f"https://t.me/{SUPPORT_USER}"),
        ],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]
    ])

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Handle referral link: /start ref_<user_id>
    referred_by = None
    if ctx.args:
        arg = ctx.args[0]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.split("_")[1])
                if ref_id != user.id:
                    referred_by = ref_id
            except (IndexError, ValueError):
                pass

    ensure_user(user.id, user.username, referred_by)

    # Notify referrer of new signup
    if referred_by:
        try:
            await ctx.bot.send_message(
                referred_by,
                f"🎉 *New Referral!*\n\n"
                f"@{user.username or user.first_name} just joined using your referral link.\n"
                f"You'll earn *{int(REFERRAL_COMMISSION*100)}%* commission on their deposits!",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"╔══════════════════════╗\n"
        f"      🏪 *TG MARKET*\n"
        f"╚══════════════════════╝\n\n"
        f"👋 Welcome, *{user.first_name}*!\n\n"
        f"The #1 marketplace to *buy* and *sell*\n"
        f"Telegram accounts safely using USD.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *How it works:*\n"
        f"  • Deposit USD to your wallet\n"
        f"  • Browse & buy Telegram accounts\n"
        f"  • List your accounts for sale\n"
        f"  • Refer friends & earn 2% commission\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Choose an option below 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ── DEPOSIT FLOW ──────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "╔══════════════════════╗\n"
        "      💰 *DEPOSIT USD*\n"
        "╚══════════════════════╝\n\n"
        "Send the amount you wish to deposit.\n\n"
        "📌 *Example:* `50`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "After submitting, the admin will verify\n"
        "your payment and credit your balance.\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✏️ Enter amount or /cancel to go back:",
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
        await update.message.reply_text(
            "❌ *Invalid amount.*\nPlease enter a positive number like `25` or `100`.",
            parse_mode="Markdown"
        )
        return DEPOSIT_AMOUNT

    with get_db() as conn:
        row = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s, %s) RETURNING id",
            (user.id, amount)
        ).fetchone()
        dep_id = row["id"]

    await ctx.bot.send_message(
        ADMIN_ID,
        f"📥 *NEW DEPOSIT REQUEST*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: @{user.username or user.first_name} (`{user.id}`)\n"
        f"💵 Amount: *${amount:.2f}*\n"
        f"🆔 Deposit ID: `{dep_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Approve: /approve_{dep_id}\n"
        f"❌ Reject:  /reject_{dep_id}",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"✅ *Deposit Request Submitted!*\n\n"
        f"💵 Amount: *${amount:.2f}*\n"
        f"🆔 Reference ID: `{dep_id}`\n\n"
        f"⏳ The admin will verify your payment\n"
        f"and credit your balance shortly.\n\n"
        f"Need help? Tap 🆘 Support in the menu.",
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

        # Pay referral commission
        referrer = conn.execute(
            "SELECT referred_by FROM users WHERE user_id=%s", (dep["user_id"],)
        ).fetchone()

        commission = 0.0
        if referrer and referrer["referred_by"]:
            commission = float(dep["amount"]) * REFERRAL_COMMISSION
            conn.execute(
                "UPDATE users SET balance=balance+%s WHERE user_id=%s",
                (commission, referrer["referred_by"])
            )
            conn.execute(
                "INSERT INTO referral_earnings (referrer_id, referred_id, deposit_id, commission) "
                "VALUES (%s, %s, %s, %s)",
                (referrer["referred_by"], dep["user_id"], dep_id, commission)
            )

    await update.message.reply_text(
        f"✅ Deposit #{dep_id} approved!\n"
        f"💵 ${dep['amount']:.2f} credited to user `{dep['user_id']}`."
        + (f"\n🤝 Referral commission ${commission:.2f} paid." if commission else ""),
        parse_mode="Markdown"
    )

    # Notify depositor
    await ctx.bot.send_message(
        dep["user_id"],
        f"🎉 *Deposit Approved!*\n\n"
        f"💵 *${dep['amount']:.2f}* has been added to your wallet.\n"
        f"🆔 Reference: `{dep_id}`\n\n"
        f"Your balance is now ready to use.\n"
        f"Start shopping! 🛒",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

    # Notify referrer of commission
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(
                referrer["referred_by"],
                f"💰 *Referral Commission Earned!*\n\n"
                f"Your referral just made a deposit.\n"
                f"You earned *${commission:.2f}* ({int(REFERRAL_COMMISSION*100)}% commission)!\n\n"
                f"Check your wallet balance 📊",
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def admin_reject_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reject_<id>")
        return

    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await update.message.reply_text("❌ Deposit not found.")
            return
        if dep["status"] != "pending":
            await update.message.reply_text("⚠️ Already processed.")
            return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))

    await update.message.reply_text(f"❌ Deposit #{dep_id} rejected.")
    await ctx.bot.send_message(
        dep["user_id"],
        f"❌ *Deposit Rejected*\n\n"
        f"Your deposit request of *${dep['amount']:.2f}* (ID: `{dep_id}`) was not approved.\n\n"
        f"Please contact 🆘 Support if you believe this is an error.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ── REFER & EARN ──────────────────────────────────────────────────────────────
async def refer_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    bot_username = ctx.bot.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    ref_count = get_referral_count(user.id)
    ref_earnings = get_referral_earnings(user.id)

    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"     👥 *REFER & EARN*\n"
        f"╚══════════════════════╝\n\n"
        f"Invite friends and earn *{int(REFERRAL_COMMISSION*100)}% commission*\n"
        f"on every deposit they make — forever!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Your Stats*\n"
        f"👥 Total Referrals: *{ref_count}*\n"
        f"💰 Total Earned:    *${ref_earnings:.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"📤 Share this link with friends.\n"
        f"When they deposit, you get 2% instantly!",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )

# ── WITHDRAW (placeholder) ────────────────────────────────────────────────────
async def withdraw_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"      💸 *WITHDRAW*\n"
        f"╚══════════════════════╝\n\n"
        f"To withdraw your balance, please contact\n"
        f"our support team directly.\n\n"
        f"💰 Your Balance: *${get_balance(query.from_user.id):.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 Contact support with your:\n"
        f"  • Withdrawal amount\n"
        f"  • Payment method\n"
        f"  • Payment details\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆘 Contact Support", url=f"https://t.me/{SUPPORT_USER}")],
            [InlineKeyboardButton("🔙 Back to Menu",    callback_data="menu_back")],
        ])
    )

# ── SELL FLOW ─────────────────────────────────────────────────────────────────
async def sell_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"    📢 *SELL AN ACCOUNT*\n"
        f"╚══════════════════════╝\n\n"
        f"List your Telegram account for sale.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✏️ Enter the Telegram username\n"
        f"you want to sell *(without @)*:\n\n"
        f"📌 Example: `username123`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Type /cancel to go back.",
        parse_mode="Markdown"
    )
    return SELL_USERNAME

async def sell_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uname = update.message.text.strip().lstrip("@")
    if not uname or len(uname) < 3:
        await update.message.reply_text(
            "❌ *Invalid username.*\nPlease enter a valid Telegram username.",
            parse_mode="Markdown"
        )
        return SELL_USERNAME
    ctx.user_data["sell_username"] = uname
    await update.message.reply_text(
        f"✅ Username: *@{uname}*\n\n"
        f"💵 Now enter your asking price in USD:\n"
        f"📌 Example: `25`",
        parse_mode="Markdown"
    )
    return SELL_PRICE

async def sell_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ *Invalid price.*\nEnter a positive number like `25` or `100`.",
            parse_mode="Markdown"
        )
        return SELL_PRICE

    ctx.user_data["sell_price"] = price
    uname = ctx.user_data["sell_username"]
    await update.message.reply_text(
        f"╔══════════════════════╗\n"
        f"     📋 *LISTING PREVIEW*\n"
        f"╚══════════════════════╝\n\n"
        f"👤 Username: *@{uname}*\n"
        f"💵 Price:    *${price:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Confirm to publish your listing?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Publish Listing", callback_data="sell_confirm")],
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
        await query.edit_message_text(
            "❌ Listing cancelled.", reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    uname = ctx.user_data["sell_username"]
    price = ctx.user_data["sell_price"]

    with get_db() as conn:
        conn.execute(
            "INSERT INTO listings (seller_id, tg_username, price) VALUES (%s, %s, %s)",
            (user.id, uname, price)
        )

    await query.edit_message_text(
        f"🎉 *Listing Published!*\n\n"
        f"👤 *@{uname}* is now live at *${price:.2f}*\n\n"
        f"You'll be notified instantly when it sells! 🔔",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


# ── BUY FLOW ──────────────────────────────────────────────────────────────────
async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    with get_db() as conn:
        listings = conn.execute(
            "SELECT l.id, l.tg_username, l.price, u.username AS seller "
            "FROM listings l JOIN users u ON l.seller_id=u.user_id "
            "WHERE l.status='active' AND l.seller_id != %s "
            "ORDER BY l.created_at DESC",
            (query.from_user.id,)
        ).fetchall()

    if not listings:
        await query.edit_message_text(
            f"╔══════════════════════╗\n"
            f"     🛒 *MARKETPLACE*\n"
            f"╚══════════════════════╝\n\n"
            f"😔 No accounts available right now.\n\n"
            f"Check back soon or list your own account\n"
            f"for sale using 📢 Sell Account!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Sell Instead", callback_data="menu_sell")],
                [InlineKeyboardButton("🔙 Back",         callback_data="menu_back")],
            ])
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"👤 @{l['tg_username']}  💵 ${l['price']:.2f}",
            callback_data=f"buy_{l['id']}"
        )]
        for l in listings
    ]
    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")])

    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"     🛒 *MARKETPLACE*\n"
        f"╚══════════════════════╝\n\n"
        f"📦 *{len(listings)} account(s) available*\n\n"
        f"Tap any listing to view details:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def buy_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split("_")[1])
    user = query.from_user
    ensure_user(user.id, user.username)

    with get_db() as conn:
        listing = conn.execute(
            "SELECT l.*, u.username AS seller_name FROM listings l "
            "JOIN users u ON l.seller_id=u.user_id "
            "WHERE l.id=%s AND l.status='active'",
            (listing_id,)
        ).fetchone()

    if not listing:
        await query.edit_message_text(
            "❌ This listing is no longer available.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Browse Others", callback_data="menu_buy")]
            ])
        )
        return

    balance   = get_balance(user.id)
    has_funds = balance >= float(listing["price"])
    status_line = "✅ You have enough funds to buy." if has_funds else "❌ Insufficient balance — deposit first."

    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"    🛒 *PURCHASE DETAILS*\n"
        f"╚══════════════════════╝\n\n"
        f"👤 Username:  *@{listing['tg_username']}*\n"
        f"💵 Price:     *${listing['price']:.2f}*\n"
        f"🧑 Seller:    @{listing['seller_name'] or 'unknown'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Your Balance: *${balance:.2f}*\n"
        f"{status_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Purchase", callback_data=f"buyconfirm_{listing_id}")],
            [InlineKeyboardButton("🔙 Back",             callback_data="menu_buy")],
        ])
    )

async def buy_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split("_")[1])
    user = query.from_user
    ensure_user(user.id, user.username)

    with get_db() as conn:
        listing = conn.execute(
            "SELECT * FROM listings WHERE id=%s AND status='active'", (listing_id,)
        ).fetchone()

        if not listing:
            await query.edit_message_text("❌ Listing no longer available.")
            return

        balance = get_balance(user.id)
        if balance < float(listing["price"]):
            await query.edit_message_text(
                f"❌ *Insufficient Balance*\n\n"
                f"💼 Your balance:  *${balance:.2f}*\n"
                f"💵 Required:      *${listing['price']:.2f}*\n\n"
                f"Please deposit funds first.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Deposit Now", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙 Back",        callback_data="menu_back")],
                ])
            )
            return

        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (listing["price"], user.id)
        )
        conn.execute(
            "UPDATE users SET balance=balance+%s WHERE user_id=%s",
            (listing["price"], listing["seller_id"])
        )
        conn.execute(
            "UPDATE listings SET status='sold', buyer_id=%s WHERE id=%s",
            (user.id, listing_id)
        )
        conn.execute(
            "INSERT INTO transactions (buyer_id, seller_id, listing_id, amount) VALUES (%s,%s,%s,%s)",
            (user.id, listing["seller_id"], listing_id, listing["price"])
        )

    await query.edit_message_text(
        f"🎉 *Purchase Successful!*\n\n"
        f"👤 Account: *@{listing['tg_username']}*\n"
        f"💵 Paid:    *${listing['price']:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"The seller has been notified and will\n"
        f"transfer the account to you shortly.\n\n"
        f"Need help? Contact 🆘 Support.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    await ctx.bot.send_message(
        listing["seller_id"],
        f"💸 *Account Sold!*\n\n"
        f"👤 *@{listing['tg_username']}* has been purchased!\n"
        f"💵 *${listing['price']:.2f}* has been added to your wallet.\n\n"
        f"Please transfer the account to the buyer.\n"
        f"Contact 🆘 Support if you need assistance.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ── WALLET & LISTINGS ─────────────────────────────────────────────────────────
async def show_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user.id, user.username)
    balance  = get_balance(user.id)
    earnings = get_referral_earnings(user.id)
    ref_count = get_referral_count(user.id)

    await query.edit_message_text(
        f"╔══════════════════════╗\n"
        f"      📊 *MY WALLET*\n"
        f"╚══════════════════════╝\n\n"
        f"💼 *Available Balance*\n"
        f"   ${balance:.2f} USD\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Referrals:        *{ref_count}*\n"
        f"🤝 Referral Earned:  *${earnings:.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 Deposit", callback_data="menu_deposit"),
                InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw"),
            ],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")],
        ])
    )

async def my_listings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE seller_id=%s ORDER BY created_at DESC",
            (user.id,)
        ).fetchall()

    if not rows:
        text = (
            f"╔══════════════════════╗\n"
            f"     📋 *MY LISTINGS*\n"
            f"╚══════════════════════╝\n\n"
            f"You haven't listed any accounts yet.\n\n"
            f"Tap 📢 Sell Account to get started!"
        )
    else:
        icons = {"active": "🟢", "sold": "✅", "cancelled": "🔴"}
        lines = [
            f"╔══════════════════════╗\n"
            f"     📋 *MY LISTINGS*\n"
            f"╚══════════════════════╝\n"
        ]
        for r in rows:
            icon = icons.get(r["status"], "⚪")
            lines.append(f"{icon} @{r['tg_username']} — *${r['price']:.2f}* `({r['status']})`")
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 New Listing", callback_data="menu_sell")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")],
        ])
    )

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    await query.edit_message_text(
        f"🏪 *TG MARKET* — Main Menu\n\n"
        f"💼 Balance: *${get_balance(user.id):.2f}*\n\n"
        f"What would you like to do?",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

# ── CANCEL ────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Action cancelled.",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

# ── ADMIN ─────────────────────────────────────────────────────────────────────
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    lines = [f"👥 *All Users* ({len(users)} total)\n━━━━━━━━━━━━━━━━━━━━━━"]
    for u in users:
        lines.append(f"• @{u['username'] or 'N/A'} (`{u['user_id']}`) — *${u['balance']:.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        deps = conn.execute(
            "SELECT d.*, u.username FROM deposits d "
            "JOIN users u ON d.user_id=u.user_id WHERE d.status='pending'"
        ).fetchall()
    if not deps:
        await update.message.reply_text("✅ No pending deposits.")
        return
    lines = [f"📥 *Pending Deposits* ({len(deps)})\n━━━━━━━━━━━━━━━━━━━━━━"]
    for d in deps:
        lines.append(
            f"🆔 `{d['id']}` — @{d['username'] or d['user_id']} — *${d['amount']:.2f}*\n"
            f"   ✅ /approve_{d['id']}   ❌ /reject_{d['id']}"
        )
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
        per_message=False,
    )
    sell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sell_start, pattern="^menu_sell$")],
        states={
            SELL_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_username)],
            SELL_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price)],
            SELL_CONFIRM:  [CallbackQueryHandler(sell_confirm, pattern="^sell_(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(deposit_conv)
    app.add_handler(sell_conv)
    app.add_handler(CallbackQueryHandler(buy_menu,      pattern="^menu_buy$"))
    app.add_handler(CallbackQueryHandler(buy_item,      pattern=r"^buy_\d+$"))
    app.add_handler(CallbackQueryHandler(buy_confirm,   pattern=r"^buyconfirm_\d+$"))
    app.add_handler(CallbackQueryHandler(show_balance,  pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(my_listings,   pattern="^menu_mylistings$"))
    app.add_handler(CallbackQueryHandler(refer_menu,    pattern="^menu_refer$"))
    app.add_handler(CallbackQueryHandler(withdraw_menu, pattern="^menu_withdraw$"))
    app.add_handler(CallbackQueryHandler(menu_back,     pattern="^menu_back$"))
    app.add_handler(CommandHandler("users",   admin_users))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/approve_\d+$") & filters.User(ADMIN_ID),
        admin_approve_deposit
    ))
    app.add_handler(MessageHandler(
        filters.Regex(r"^/reject_\d+$") & filters.User(ADMIN_ID),
        admin_reject_deposit
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
