import os
import logging
import traceback
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
BOT_TOKEN        = os.getenv("BOT_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
DATABASE_URL     = os.getenv("DATABASE_URL", "")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")
PORT             = int(os.getenv("PORT", "8080"))
API_ID           = int(os.getenv("API_ID", "0"))
API_HASH         = os.getenv("API_HASH", "")
REFERRAL_COMMISSION = 0.02

# ── Channel IDs ───────────────────────────────────────────────────────────────
# FUNDS_CHANNEL  : deposit & withdrawal requests go here (admin approves from here)
# TRADES_CHANNEL : buy/sell/add account activity goes here
FUNDS_CHANNEL  = int(os.getenv("FUNDS_CHANNEL_ID",  "0"))
TRADES_CHANNEL = int(os.getenv("TRADES_CHANNEL_ID", "0"))

ptb_app: Application = None

# ── Channel helper ────────────────────────────────────────────────────────────
async def send_to_channel(bot, channel_id: int, text: str, parse_mode: str = "HTML", reply_markup=None):
    """Send a message to a channel. Silently skips if channel_id is 0 or not configured."""
    if not channel_id:
        return
    try:
        await bot.send_message(channel_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Channel send failed (channel={channel_id}): {e}")

# ── Unicode bold text helper ──────────────────────────────────────────────────
_BM = {}
for _i, _c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _BM[_c] = chr(0x1D5D4 + _i)
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BM[_c] = chr(0x1D5EE + _i)
for _i, _c in enumerate("0123456789"):
    _BM[_c] = chr(0x1D7EC + _i)

def b(text: str) -> str:
    """Convert text to Unicode Mathematical Bold Sans-Serif — works in buttons too."""
    return "".join(_BM.get(c, c) for c in text)

def mask_phone(phone: str) -> str:
    """Format phone as +923*******19 — shows first 3 and last 2 digits, masks the rest."""
    p = phone.strip().lstrip("+")
    if len(p) < 6:
        return phone  # too short to mask
    visible_start = p[:3]
    visible_end   = p[-2:]
    masked        = "*" * (len(p) - 5)
    return f"+{visible_start}{masked}{visible_end}"

def phone_to_country(phone: str) -> tuple:
    """Return (flag_emoji, country_name) by matching dial code from country_prices table.
    Falls back to generic globe if not found."""
    try:
        p = phone.strip().lstrip("+")
        with get_db() as conn:
            rows = conn.execute(
                "SELECT country_code, country_name, dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
            ).fetchall()
        for r in rows:
            if p.startswith(r["dial_code"]):
                flag = country_flag(r["country_code"])
                return flag, r["country_name"]
    except Exception:
        pass
    return "🌍", "Unknown"

(DEPOSIT_AMOUNT, ADMIN_PHONE, ADMIN_OTP, ADMIN_ADD_PRICE,
 SELL_PHONE, SELL_OTP, SELL_PRICE, WITHDRAW_UPI, WITHDRAW_AMOUNT,
 DEPOSIT_SCREENSHOT) = range(10)

# ── Payment details (set these in Render env vars) ────────────────────────────
PAYMENT_UPI    = os.getenv("PAYMENT_UPI", "yourname@upi")
PAYMENT_QR     = os.getenv("PAYMENT_QR_FILE_ID", "")   # Telegram file_id (optional)
PAYMENT_QR_PATH = os.getenv("PAYMENT_QR_PATH", "qr.png")  # local image file path

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT DEFAULT '',
            balance NUMERIC(12,2) DEFAULT 0, referred_by BIGINT DEFAULT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT DEFAULT NULL")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS deposits (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount NUMERIC(12,2),
            status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id BIGSERIAL PRIMARY KEY, session TEXT, phone TEXT DEFAULT '',
            price NUMERIC(12,2),
            status TEXT DEFAULT 'available', buyer_id BIGINT DEFAULT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS referral_earnings (
            id BIGSERIAL PRIMARY KEY, referrer_id BIGINT, referred_id BIGINT,
            deposit_id BIGINT, commission NUMERIC(12,2),
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("""CREATE TABLE IF NOT EXISTS country_prices (
            country_code TEXT PRIMARY KEY,
            country_name TEXT NOT NULL,
            dial_code    TEXT NOT NULL DEFAULT '',
            price        NUMERIC(12,2) NOT NULL,
            updated_at   TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE country_prices ADD COLUMN IF NOT EXISTS dial_code TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            amount NUMERIC(12,2),
            upi_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        conn.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS upi_id TEXT DEFAULT ''")
    logger.info("✅ Database initialised.")

# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_user(user_id: int, username: str = "", referred_by: int = None):
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,)).fetchone():
            conn.execute(
                "INSERT INTO users (user_id, username, referred_by) VALUES (%s,%s,%s)",
                (user_id, username or "", referred_by)
            )

def get_balance(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()
        return float(row["balance"]) if row else 0.0

def get_referral_count(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE referred_by=%s", (user_id,)).fetchone()
        return row["cnt"] if row else 0

def get_referral_earnings(user_id: int) -> float:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(commission),0) AS total FROM referral_earnings WHERE referrer_id=%s",
            (user_id,)
        ).fetchone()
        return float(row["total"]) if row else 0.0

# ── Premium emoji helpers ──────────────────────────────────────────────────────
def pe(emoji_id: str, fallback: str) -> str:
    """Wrap a premium emoji ID for use in HTML parse_mode messages."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# Premium emoji IDs
PE_BUY      = "6298691319086712919"
PE_SELL     = "6298356878573307709"
PE_RECHARGE = "6255738287462288807"
PE_WITHDRAW = "6129731974291527294"
PE_WALLET   = "6129801569941592173"
PE_REFER    = "6129700535130922338"
PE_SUPPORT  = "6296577138615125756"

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒  Buy Account",    callback_data="menu_buy"),
         InlineKeyboardButton("💰  Sell Account",   callback_data="menu_sell")],
        [InlineKeyboardButton("💵  Recharge",       callback_data="menu_deposit"),
         InlineKeyboardButton("💸  Withdraw",       callback_data="menu_withdraw")],
        [InlineKeyboardButton("📊  My Wallet",      callback_data="menu_balance"),
         InlineKeyboardButton("👥  Refer & Earn",   callback_data="menu_refer")],
        [InlineKeyboardButton("🆘  Support",        url=f"https://t.me/{SUPPORT_USERNAME}")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]])


# ── PTB error handler (logs ALL handler exceptions to console) ────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=ctx.error)
    logger.error(traceback.format_exc())
    # Try to notify the user something went wrong
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again or contact support."
            )
    except Exception:
        pass


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start from user_id={user.id} username={user.username}")
    referred_by = None
    if ctx.args and ctx.args[0].startswith("ref_"):
        try:
            ref_id = int(ctx.args[0].split("_")[1])
            if ref_id != user.id:
                referred_by = ref_id
        except (IndexError, ValueError):
            pass
    try:
        ensure_user(user.id, user.username or "", referred_by)
    except Exception as e:
        logger.error(f"ensure_user failed: {e}\n{traceback.format_exc()}")
    if referred_by:
        try:
            await ctx.bot.send_message(referred_by,
                f"<b>🎉 New Referral!</b>\n\n"
                f"<b>@{user.username or user.first_name}</b> just joined using your link!\n"
                f"You'll earn <b>{int(REFERRAL_COMMISSION*100)}%</b> on their deposits.",
                parse_mode="HTML")
        except Exception:
            pass
    await update.message.reply_text(
        f"<b>⚡ TG MARKET — Official Bot</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"👋 <b>Welcome, {user.first_name}!</b>\n\n"
        f"<b>The #1 trusted marketplace</b> to buy &amp; sell\n"
        f"Telegram accounts securely using <b>USD</b>.\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💡 How It Works</b>\n\n"
        f"  <b>💵</b>  Recharge USD to your wallet\n"
        f"  <b>🛒</b>  Browse &amp; buy Telegram accounts\n"
        f"  <b>🔑</b>  Receive session instantly after purchase\n"
        f"  <b>👥</b>  Refer friends &amp; earn <b>2% commission</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>🔒 Secure  •  Fast  •  Trusted</b>\n\n"
        f"Select an option below 👇",
        parse_mode="HTML", reply_markup=main_menu_keyboard())

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal = get_balance(query.from_user.id)
    text = (
        f"<b>⚡ TG MARKET — Main Menu</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💼 Wallet Balance</b>\n"
        f"<b>💲 ${bal:.2f} USD</b>\n\n"
        f"<b>🔒 Secure  •  Fast  •  Trusted</b>\n\n"
        f"What would you like to do?"
    )
    # If the message has a photo/caption, delete it and send a fresh message
    # instead of trying to edit (edit_message_text fails on photo messages)
    try:
        if query.message.photo or query.message.document:
            await query.message.delete()
            await ctx.bot.send_message(
                query.from_user.id, text,
                parse_mode="HTML", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    except Exception:
        # Fallback: always send a new message
        await ctx.bot.send_message(
            query.from_user.id, text,
            parse_mode="HTML", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ <b>Action cancelled.</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── DEPOSIT ───────────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 — Ask how much they want to deposit."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"<b>💵 RECHARGE WALLET</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"Enter the amount in <b>USD</b> you want to deposit.\n\n"
        f"📌 <b>Example:</b> <code>50</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"✏️ <b>Type the amount below</b> or /cancel to go back:",
        parse_mode="HTML")
    return DEPOSIT_AMOUNT

async def deposit_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Show UPI + QR code and ask for payment screenshot."""
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>❌ Invalid amount.</b> Enter a positive number like <code>25</code>.",
            parse_mode="HTML")
        return DEPOSIT_AMOUNT

    ctx.user_data["dep_amount"] = amount

    msg = (
        f"<b>💵 Amount to Pay: ${amount:.2f}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📲 Pay via UPI:</b>\n"
        f"<code>{PAYMENT_UPI}</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"📸 After payment, send the <b>screenshot</b> of your payment here.\n\n"
        f"⚠️ <b>Your deposit will be credited after admin verifies the screenshot.</b>"
    )

    if PAYMENT_QR:
        # Send QR using Telegram file_id
        await update.message.reply_photo(
            photo=PAYMENT_QR,
            caption=msg,
            parse_mode="HTML")
    elif os.path.isfile(PAYMENT_QR_PATH):
        # Send QR directly from local image file
        with open(PAYMENT_QR_PATH, "rb") as qr_file:
            await update.message.reply_photo(
                photo=qr_file,
                caption=msg,
                parse_mode="HTML")
    else:
        await update.message.reply_text(msg, parse_mode="HTML")

    return DEPOSIT_SCREENSHOT

async def deposit_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Receive screenshot, create deposit record, notify admin with inline buttons."""
    user   = update.effective_user
    amount = ctx.user_data.get("dep_amount", 0)

    if not update.message.photo:
        await update.message.reply_text(
            "<b>❌ Please send a screenshot photo</b> of your payment.",
            parse_mode="HTML")
        return DEPOSIT_SCREENSHOT

    photo_id = update.message.photo[-1].file_id

    with get_db() as conn:
        dep_id = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s,%s) RETURNING id",
            (user.id, amount)
        ).fetchone()["id"]

    # Inline buttons for admin
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"dep_approve_{dep_id}"),
         InlineKeyboardButton("❌ Reject",  callback_data=f"dep_reject_{dep_id}")]
    ])

    admin_caption = (
        f"<b>📥 NEW DEPOSIT REQUEST</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>👤 User:</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
        f"<b>💵 Amount: ${amount:.2f}</b>\n"
        f"<b>🆔 Deposit ID:</b> <code>{dep_id}</code>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"📸 <b>Payment screenshot attached.</b>"
    )

    # Send screenshot + buttons to admin only
    await ctx.bot.send_photo(
        ADMIN_ID,
        photo=photo_id,
        caption=admin_caption,
        parse_mode="HTML",
        reply_markup=admin_kb)

    # Channel gets NO screenshot — just basic info
    await send_to_channel(ctx.bot, FUNDS_CHANNEL,
        f"📥 <b>DEPOSIT REQUEST</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"💵 Amount: <b>${amount:.2f}</b>\n"
        f"🔖 Deposit ID: <code>{dep_id}</code>\n"
        f"📊 Status: <b>⏳ Pending</b>"
    )

    await update.message.reply_text(
        f"<b>✅ Screenshot Received!</b>\n\n"
        f"<b>💵 Amount: ${amount:.2f}</b>\n"
        f"<b>🆔 Reference ID:</b> <code>{dep_id}</code>\n\n"
        f"⏳ <b>Admin will verify your payment and credit your balance shortly.</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── Deposit inline approve/reject (callback buttons) ─────────────────────────
async def dep_approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ✅ Approve on deposit message."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    dep_id = int(query.data.split("_")[2])
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await query.answer("Not found.", show_alert=True); return
        if dep["status"] != "pending":
            await query.answer("Already processed.", show_alert=True); return
        conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (dep["amount"], dep["user_id"]))
        referrer = conn.execute("SELECT referred_by FROM users WHERE user_id=%s", (dep["user_id"],)).fetchone()
        commission = 0.0
        if referrer and referrer["referred_by"]:
            commission = float(dep["amount"]) * REFERRAL_COMMISSION
            conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (commission, referrer["referred_by"]))
            conn.execute("INSERT INTO referral_earnings (referrer_id,referred_id,deposit_id,commission) VALUES (%s,%s,%s,%s)",
                (referrer["referred_by"], dep["user_id"], dep_id, commission))
    await query.edit_message_caption(
        caption=f"<b>✅ Deposit #{dep_id} — APPROVED</b>\n<b>💵 ${dep['amount']:.2f}</b> credited to <code>{dep['user_id']}</code>",
        parse_mode="HTML")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>🎉 Deposit Approved!</b>\n\n"
        f"<b>💵 ${dep['amount']:.2f}</b> has been added to your wallet.\n"
        f"<b>🆔 Ref:</b> <code>{dep_id}</code>\n\n"
        f"<b>Start shopping now! 🛒</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, FUNDS_CHANNEL,
        f"✅ <b>DEPOSIT APPROVED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Deposit ID: <code>{dep_id}</code>\n"
        f"🆔 User ID: <code>{dep['user_id']}</code>\n"
        f"💵 Amount: <b>${dep['amount']:.2f}</b>\n"
        f"📊 Status: <b>✅ Approved</b>"
        + (f"\n🤝 Referral: <b>${commission:.2f}</b>" if commission else ""))
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(referrer["referred_by"],
                f"<b>💰 Referral Commission Earned!</b>\n\nYou earned <b>${commission:.2f}</b>!", parse_mode="HTML")
        except Exception:
            pass
    await query.answer("✅ Approved!")

async def dep_reject_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ❌ Reject on deposit message."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True); return
    dep_id = int(query.data.split("_")[2])
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await query.answer("Not found or already processed.", show_alert=True); return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))
    await query.edit_message_caption(
        caption=f"<b>❌ Deposit #{dep_id} — REJECTED</b>",
        parse_mode="HTML")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>❌ Deposit Rejected</b>\n\n"
        f"Your deposit of <b>${dep['amount']:.2f}</b> (ID: <code>{dep_id}</code>) was not approved.\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, FUNDS_CHANNEL,
        f"❌ <b>DEPOSIT REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Deposit ID: <code>{dep_id}</code>\n"
        f"🆔 User ID: <code>{dep['user_id']}</code>\n"
        f"💵 Amount: <b>${dep['amount']:.2f}</b>\n"
        f"📊 Status: <b>❌ Rejected</b>")
    await query.answer("❌ Rejected.")

async def admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Legacy text command fallback: /approve_<id>"""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve_<id>"); return
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep:
            await update.message.reply_text("❌ Not found."); return
        if dep["status"] != "pending":
            await update.message.reply_text("⚠️ Already processed."); return
        conn.execute("UPDATE deposits SET status='approved' WHERE id=%s", (dep_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (dep["amount"], dep["user_id"]))
        referrer = conn.execute("SELECT referred_by FROM users WHERE user_id=%s", (dep["user_id"],)).fetchone()
        commission = 0.0
        if referrer and referrer["referred_by"]:
            commission = float(dep["amount"]) * REFERRAL_COMMISSION
            conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (commission, referrer["referred_by"]))
            conn.execute("INSERT INTO referral_earnings (referrer_id,referred_id,deposit_id,commission) VALUES (%s,%s,%s,%s)",
                (referrer["referred_by"], dep["user_id"], dep_id, commission))
    await update.message.reply_text(
        f"✅ Deposit #{dep_id} approved! <b>${dep['amount']:.2f}</b> credited."
        + (f"\n🤝 Referral <b>${commission:.2f}</b> paid." if commission else ""), parse_mode="HTML")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>🎉 Deposit Approved!</b>\n\n"
        f"<b>💵 ${dep['amount']:.2f}</b> added to your wallet.\n"
        f"<b>🆔 Ref:</b> <code>{dep_id}</code>\n\n"
        f"<b>Start shopping! 🛒</b>",
        parse_mode="HTML", reply_markup=main_menu_keyboard())
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(referrer["referred_by"],
                f"<b>💰 Referral Commission Earned!</b>\nYou earned <b>${commission:.2f}</b>!", parse_mode="HTML")
        except Exception:
            pass

async def admin_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Legacy text command fallback: /reject_<id>"""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        dep_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reject_<id>"); return
    with get_db() as conn:
        dep = conn.execute("SELECT * FROM deposits WHERE id=%s", (dep_id,)).fetchone()
        if not dep or dep["status"] != "pending":
            await update.message.reply_text("❌ Not found or already processed."); return
        conn.execute("UPDATE deposits SET status='rejected' WHERE id=%s", (dep_id,))
    await update.message.reply_text(f"❌ Deposit #{dep_id} rejected.")
    await ctx.bot.send_message(dep["user_id"],
        f"<b>❌ Deposit Rejected</b>\n\n"
        f"Your deposit of <b>${dep['amount']:.2f}</b> (ID: <code>{dep_id}</code>) was not approved.\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML", reply_markup=main_menu_keyboard())

# ── Admin: credit / deduct ────────────────────────────────────────────────────
async def admin_credit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        user_id = int(parts[1])
        amount  = float(parts[2])
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: <code>/credit &lt;user_id&gt; &lt;amount&gt;</code>\nExample: <code>/credit 123456789 50</code>", parse_mode="HTML")
        return
    ensure_user(user_id, "")
    with get_db() as conn:
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ <b>${amount:.2f} credited to <code>{user_id}</code></b>\nNew balance: <b>${new_bal:.2f}</b>", parse_mode="HTML")
    try:
        await ctx.bot.send_message(user_id,
            f"<b>🎉 ${amount:.2f} added to your balance by admin!</b>\n\nNew balance: <b>${new_bal:.2f}</b>\n\n<b>Start shopping! 🛒</b>",
            parse_mode="HTML", reply_markup=main_menu_keyboard())
    except Exception:
        await update.message.reply_text("⚠️ Credited but could not notify user.")

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
        await update.message.reply_text("Usage: <code>/deduct &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode="HTML")
        return
    with get_db() as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()
        if not bal or float(bal["balance"]) < amount:
            await update.message.reply_text("❌ User not found or insufficient balance."); return
        conn.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ <b>${amount:.2f} deducted from <code>{user_id}</code></b>\nNew balance: <b>${new_bal:.2f}</b>", parse_mode="HTML")


# ── Admin: login via OTP ──────────────────────────────────────────────────────
async def admin_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>📱 LOGIN ACCOUNT</b>\n"
        "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        "Send the phone number with country code.\n\n"
        "<b>📌 Example:</b> <code>+12345678900</code>\n\n"
        "/cancel to abort.",
        parse_mode="HTML")
    return ADMIN_PHONE

async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    ctx.user_data["phone"] = phone
    await update.message.reply_text("⏳ Sending OTP...")
    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "<b>❌ Configuration Error</b>\n\n<code>API_ID</code> or <code>API_HASH</code> not set in Render env vars.",
            parse_mode="HTML")
        return ConversationHandler.END
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        ctx.user_data["client"] = client
        ctx.user_data["phone_code_hash"] = result.phone_code_hash
        await update.message.reply_text(
            "<b>📩 OTP sent!</b>\n\nEnter the OTP you received <i>(digits only, e.g. <code>12345</code>)</i>:",
            parse_mode="HTML")
        return ADMIN_OTP
    except Exception as e:
        logger.error(f"OTP error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"<b>❌ Failed to send OTP</b>\n\n<code>{type(e).__name__}: {e}</code>\n\n"
            f"• <b>API_ID set:</b> <code>{'Yes' if API_ID else 'No'}</code>\n"
            f"• <b>API_HASH set:</b> <code>{'Yes' if API_HASH else 'No'}</code>",
            parse_mode="HTML")
        return ConversationHandler.END

async def get_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    otp       = update.message.text.strip().replace(" ", "")
    client    = ctx.user_data.get("client")
    phone     = ctx.user_data.get("phone")
    code_hash = ctx.user_data.get("phone_code_hash")
    if not client:
        await update.message.reply_text("❌ Session expired. Run /login_account again.")
        return ConversationHandler.END
    try:
        from telethon.sessions import StringSession
        await client.sign_in(phone, otp, phone_code_hash=code_hash)
        session_string = client.session.save()
        await client.disconnect()
        ctx.user_data["session"] = session_string
        await update.message.reply_text(
            "<b>✅ Login successful!</b>\n\n<b>💵 Now enter the price</b> for this account (e.g. <code>25</code>):",
            parse_mode="HTML")
        return ADMIN_ADD_PRICE
    except Exception as e:
        await update.message.reply_text(f"<b>❌ Login failed</b>\n\n<code>{e}</code>\n\nRun /login_account to try again.", parse_mode="HTML")
        return ConversationHandler.END

async def set_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Enter a valid positive price.")
        return ADMIN_ADD_PRICE
    with get_db() as conn:
        acc_id = conn.execute(
            "INSERT INTO accounts (session, phone, price) VALUES (%s,%s,%s) RETURNING id",
            (ctx.user_data["session"], ctx.user_data.get("phone", ""), price)
        ).fetchone()["id"]
    await update.message.reply_text(
        f"<b>🎉 Account #{acc_id} Added!</b>\n\n<b>💵 Price: ${price:.2f}</b>\n🟢 <b>Now visible in the marketplace.</b>",
        parse_mode="HTML")
    # Post to trades channel
    phone_raw = ctx.user_data.get("phone", "")
    flag, country_name = phone_to_country(phone_raw)
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"➕ <b>NEW ACCOUNT ADDED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country {flag} {country_name}\n"
        f"� Phone: <code>{mask_phone(phone_raw)}</code>\n"
        f"💵 Price: <b>${price:.2f}</b>\n"
        f"📊 Stock: 1\n"
        f"📊 Status: 🟢 Available\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Now go to Buy Account to grab it!"
    )
    return ConversationHandler.END

# ── Admin: view commands ──────────────────────────────────────────────────────
async def admin_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        rows = conn.execute("SELECT id, price, status, buyer_id FROM accounts ORDER BY id DESC").fetchall()
    if not rows:
        await update.message.reply_text("📦 No accounts yet."); return
    icons = {"available": "🟢", "sold": "✅", "pending_review": "🔄"}
    lines = [f"<b>📦 All Accounts ({len(rows)})</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    for r in rows:
        lines.append(f"{icons.get(r['status'],'⚪')} <b>#{r['id']}</b> — <b>${r['price']:.2f}</b> ({r['status']})"
            + (f" → <code>{r['buyer_id']}</code>" if r["buyer_id"] else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    lines = [f"<b>👥 All Users ({len(users)})</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    for u in users:
        lines.append(f"• @{u['username'] or 'N/A'} (<code>{u['user_id']}</code>) — <b>${u['balance']:.2f}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        deps = conn.execute(
            "SELECT d.*, u.username FROM deposits d JOIN users u ON d.user_id=u.user_id WHERE d.status='pending'"
        ).fetchall()
    if not deps:
        await update.message.reply_text("✅ No pending deposits."); return
    lines = [f"<b>📥 Pending Deposits ({len(deps)})</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"]
    for d in deps:
        lines.append(f"<b>🆔</b> <code>{d['id']}</code> — @{d['username'] or d['user_id']} — <b>${d['amount']:.2f}</b>\n"
            f"   ✅ /approve_{d['id']}   ❌ /reject_{d['id']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def admin_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        acc_id = int(update.message.text.split("_")[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /del_<id>"); return
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id=%s", (acc_id,))
    await update.message.reply_text(f"🗑 Account #{acc_id} deleted.")

async def admin_add_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sets price for a pending_review account submitted by a seller.
    Usage: /add_sell <account_id> <price>
    The account_id is shown in the notification sent when user submits.
    """
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split()
        acc_id = int(parts[1])
        price  = float(parts[2])
        if price <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: <code>/add_sell &lt;account_id&gt; &lt;price&gt;</code>\n"
            "Example: <code>/add_sell 5 25.00</code>",
            parse_mode="HTML")
        return
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
        if not acc:
            await update.message.reply_text(
                f"❌ Account #{acc_id} not found or not pending review."); return
        conn.execute(
            "UPDATE accounts SET price=%s, status='available' WHERE id=%s",
            (price, acc_id)
        )
    await update.message.reply_text(
        f"✅ Account #{acc_id} listed at <b>${price:.2f}</b> — now visible in marketplace.",
        parse_mode="HTML"
    )

# ── OTP background watcher ────────────────────────────────────────────────────
async def _watch_for_otp(bot, user_id: int, session_str: str, phone: str, acc_id: int):
    """
    Runs in the background after a purchase.
    Connects with the sold account's session, waits for a new message from
    Telegram's service account (777000), extracts the OTP, and forwards it
    to the buyer. Does NOT trigger the OTP itself — the user does that by
    entering the phone number on their own device.
    """
    import re
    import asyncio
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetHistoryRequest

    try:
        session_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await session_client.connect()

        # Snapshot the latest message ID from 777000 right now (before OTP arrives)
        baseline_id = 0
        service_peer = None
        try:
            service_peer = await session_client.get_input_entity(777000)
            history = await session_client(GetHistoryRequest(
                peer=service_peer,
                limit=1,
                offset_date=None, offset_id=0,
                max_id=0, min_id=0, add_offset=0, hash=0
            ))
            if history.messages:
                baseline_id = history.messages[0].id
        except Exception as e:
            logger.warning(f"[OTP watcher #{acc_id}] baseline error: {e}")

        # Poll every 4 seconds for up to 5 minutes
        otp_code = None
        deadline = asyncio.get_event_loop().time() + 300

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(4)
            try:
                history = await session_client(GetHistoryRequest(
                    peer=service_peer,
                    limit=5,
                    offset_date=None, offset_id=0,
                    max_id=0, min_id=0, add_offset=0, hash=0
                ))
                for msg in history.messages:
                    if msg.id <= baseline_id:
                        continue  # skip old messages
                    text = getattr(msg, "message", "") or ""
                    match = re.search(r'\b(\d{5,6})\b', text)
                    if match:
                        otp_code = match.group(1)
                        break
            except Exception as e:
                logger.warning(f"[OTP watcher #{acc_id}] poll error: {e}")
            if otp_code:
                break

        await session_client.disconnect()

        if otp_code:
            await bot.send_message(
                user_id,
                f"<b>🔐 Your OTP Has Arrived!</b>\n\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"<b>📱 Phone:</b> <code>{phone}</code>\n"
                f"<b>🔑 OTP Code:</b> <code>{otp_code}</code>\n"
                f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
                f"⚠️ <b>Enter this code in Telegram now.</b>\n"
                f"⚠️ <b>OTP expires in a few minutes.</b>\n"
                f"⚠️ <b>Do NOT share these details with anyone.</b>",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                user_id,
                f"<b>⏰ OTP Not Detected Automatically</b>\n\n"
                f"Telegram may have sent the code via SMS instead.\n\n"
                f"<b>📱 Phone:</b> <code>{phone}</code>\n\n"
                f"Please check your SMS or contact support.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆘  Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
                ])
            )

    except Exception as e:
        logger.error(f"[OTP watcher #{acc_id}] fatal: {e}\n{traceback.format_exc()}")
        await bot.send_message(
            user_id,
            f"<b>⚠️ OTP Auto-Detection Failed</b>\n\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n\n"
            f"Please request the OTP manually and contact support if needed.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆘  Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
            ])
        )


# ── BUY FLOW ──────────────────────────────────────────────────────────────────
async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show available accounts grouped by country as inline buttons."""
    query = update.callback_query
    await query.answer()

    with get_db() as conn:
        # Get all available accounts with their phone numbers
        accounts = conn.execute(
            "SELECT id, price, phone FROM accounts WHERE status='available' ORDER BY price ASC"
        ).fetchall()
        # Get country_prices for dial code lookup
        countries = conn.execute(
            "SELECT country_code, country_name, dial_code FROM country_prices ORDER BY LENGTH(dial_code) DESC"
        ).fetchall()

    if not accounts:
        await query.edit_message_text(
            "<b>🛒 MARKETPLACE</b>\n"
            "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
            "<b>😔 No accounts available right now.</b>\nCheck back soon!",
            parse_mode="HTML", reply_markup=back_keyboard())
        return

    # Group accounts by country using dial code matching
    def get_country(phone):
        p = phone.strip().lstrip("+")
        for c in countries:
            if p.startswith(c["dial_code"]):
                return c["country_code"], c["country_name"], c["dial_code"]
        return "XX", "Other", "0"

    country_counts = {}  # {country_code: {name, dial, count, price_min}}
    for acc in accounts:
        code, name, dial = get_country(acc["phone"] or "")
        if code not in country_counts:
            country_counts[code] = {"name": name, "dial": dial, "count": 0, "price": float(acc["price"])}
        country_counts[code]["count"] += 1
        country_counts[code]["price"] = min(country_counts[code]["price"], float(acc["price"]))

    # Build one button per country
    buttons = []
    for code, info in sorted(country_counts.items(), key=lambda x: x[1]["name"]):
        flag = country_flag(code)
        label = f"+{info['dial']} : {flag} {info['name']} [ {info['count']} Available ]"
        buttons.append([InlineKeyboardButton(label, callback_data=f"buycountry_{code}")])

    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")])

    total = sum(v["count"] for v in country_counts.values())
    await query.edit_message_text(
        f"<b>🛒 MARKETPLACE</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>📦 {total} account(s) available</b>\n\n"
        f"Select a country to browse accounts:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons))


async def buy_country(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show individual accounts for a selected country."""
    query = update.callback_query
    await query.answer()
    country_code = query.data.split("buycountry_")[1]

    with get_db() as conn:
        # Get country info
        country = conn.execute(
            "SELECT country_name, dial_code FROM country_prices WHERE country_code=%s",
            (country_code,)
        ).fetchone()
        # Get available accounts for this country by matching dial code
        all_accs = conn.execute(
            "SELECT id, price, phone FROM accounts WHERE status='available' ORDER BY price ASC"
        ).fetchall()

    if country:
        dial = country["dial_code"]
        name = country["country_name"]
        flag = country_flag(country_code)
    else:
        dial, name, flag = "0", "Other", "🌍"

    # Filter accounts matching this country's dial code
    accs = [a for a in all_accs if (a["phone"] or "").strip().lstrip("+").startswith(dial)]

    if not accs:
        await query.edit_message_text(
            f"😔 No {flag} {name} accounts available right now.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu_buy")]
            ]))
        return

    buttons = []
    for a in accs:
        buttons.append([InlineKeyboardButton(
            f"🔑 Account #{a['id']}  —  ${a['price']:.2f}",
            callback_data=f"view_{a['id']}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_buy")])

    await query.edit_message_text(
        f"<b>🛒 MARKETPLACE</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"{flag} <b>{name}</b> accounts\n"
        f"<b>📦 {len(accs)} available</b>\n\n"
        f"Tap any account to view details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons))

async def view_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id  = int(query.data.split("_")[1])
    user_id = query.from_user.id
    ensure_user(user_id, query.from_user.username or "")
    with get_db() as conn:
        acc = conn.execute("SELECT id, price FROM accounts WHERE id=%s AND status='available'", (acc_id,)).fetchone()
    if not acc:
        await query.edit_message_text("❌ No longer available.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Browse Others", callback_data="menu_buy")]])); return
    balance = get_balance(user_id)
    has_funds = balance >= float(acc["price"])
    await query.edit_message_text(
        f"<b>🔑 ACCOUNT DETAILS</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>🆔 Account ID:  #{acc['id']}</b>\n"
        f"<b>💵 Price:       ${acc['price']:.2f}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💼 Your Balance: ${balance:.2f}</b>\n"
        f"{'<b>✅ You have enough funds.</b>' if has_funds else '<b>❌ Insufficient balance — deposit first.</b>'}\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅  Buy Now", callback_data=f"confirm_{acc_id}")],
            [InlineKeyboardButton("🔙  Back",    callback_data="menu_buy")]]))

async def confirm_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id  = int(query.data.split("_")[1])
    user_id = query.from_user.id
    ensure_user(user_id, query.from_user.username or "")

    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='available'", (acc_id,)
        ).fetchone()
        if not acc:
            await query.edit_message_text("❌ Account no longer available."); return

        balance = get_balance(user_id)
        if balance < float(acc["price"]):
            await query.edit_message_text(
                f"<b>❌ Insufficient Balance</b>\n\n"
                f"<b>💼 Your balance: ${balance:.2f}</b>\n"
                f"<b>💵 Required:     ${acc['price']:.2f}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰  Deposit Now", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙  Back",        callback_data="menu_back")]
                ])
            )
            return

        # Deduct balance and mark sold
        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s", (acc["price"], user_id)
        )
        conn.execute(
            "UPDATE accounts SET status='sold', buyer_id=%s WHERE id=%s", (user_id, acc_id)
        )

    await query.edit_message_text(
        f"<b>🎉 Purchase Successful!</b>\n\n"
        f"<b>🔑 Account #{acc_id} is yours!</b>\n"
        f"<b>💵 Paid: ${acc['price']:.2f}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"⏳ <b>Sending login details...</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

    phone = acc.get("phone", "").strip()

    # ── Send phone number to buyer immediately ────────────────────────────────
    await ctx.bot.send_message(
        user_id,
        f"<b>📱 Your Account Phone Number</b>\n\n"
        f"<code>{phone}</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>How to login:</b>\n"
        f"<b>1️⃣</b>  Open Telegram on any device\n"
        f"<b>2️⃣</b>  Enter the phone number above\n"
        f"<b>3️⃣</b>  Telegram will send an OTP to this account\n"
        f"<b>4️⃣</b>  I will automatically forward the OTP to you here ⬇️\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"⏳ <b>Waiting for OTP... (up to 5 minutes)</b>",
        parse_mode="HTML"
    )

    # ── Launch OTP watcher as a background task on the shared event loop ─────
    # Must NOT await — the webhook has no timeout now but we still want this
    # running independently so it doesn't block other updates.
    # Use the same loop the webhook runs on (stored in flask_app.config).
    import asyncio
    bg_loop = flask_app.config.get("ASYNCIO_LOOP")
    if bg_loop:
        asyncio.run_coroutine_threadsafe(
            _watch_for_otp(ctx.bot, user_id, acc["session"], phone, acc_id),
            bg_loop
        )
    else:
        # Fallback: schedule on current loop
        asyncio.get_event_loop().create_task(
            _watch_for_otp(ctx.bot, user_id, acc["session"], phone, acc_id)
        )

    # Notify admin
    await ctx.bot.send_message(
        ADMIN_ID,
        f"<b>💸 Account Sold</b>\n\n"
        f"<b>🔑 Account #{acc_id}</b> sold to <code>{user_id}</code> for <b>${acc['price']:.2f}</b>.\n"
        f"<b>📱 Phone:</b> <code>{phone}</code>",
        parse_mode="HTML"
    )
    # Post to trades channel
    buyer = await ctx.bot.get_chat(user_id)
    buyer_name = f"@{buyer.username}" if buyer.username else buyer.first_name
    flag, country_name = phone_to_country(phone)
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"🛒 <b>ACCOUNT SOLD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country {flag} {country_name}\n"
        f"� Phone: <code>{mask_phone(phone)}</code>\n"
        f"💵 Price: <b>${acc['price']:.2f}</b>\n"
        f"👤 Buyer: {buyer_name} (<code>{user_id}</code>)\n"
        f"📊 Status: ✅ Sold\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ── PRICES ────────────────────────────────────────────────────────────────────
# Country flag emoji helper (converts country code to flag emoji)
def country_flag(code: str) -> str:
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper()[:2])
    except Exception:
        return "🌍"

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Public /prices command — shows the buy price list."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    if not rows:
        await update.message.reply_text(
            "No prices available yet. Check back soon!",
            reply_markup=main_menu_keyboard())
        return
    lines = ["<b>💰 We Buy From You — Price List</b>\n<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"]
    for r in rows:
        flag = country_flag(r["country_code"])
        lines.append(
            f"<b>{flag} +{r['dial_code']}-{r['country_code']}:</b>  <b>{r['price']}$</b>  <i>({r['country_name']})</i>"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰  Sell Account", callback_data="menu_sell")]
        ])
    )


# ── ADMIN PRICE PANEL ─────────────────────────────────────────────────────────
(APANEL_ADD_WAITING, APANEL_EDIT_WAITING, APANEL_DEL_CONFIRM) = range(10, 13)
SELL_APPROVE_PRICE = 13  # state: admin enters price after approving a sell submission

def _prices_panel_keyboard(rows):
    """Build the admin price panel keyboard."""
    buttons = []
    for r in rows:
        flag = country_flag(r["country_code"])
        buttons.append([
            InlineKeyboardButton(
                f"{flag} {r['country_code']} +{r['dial_code']} — {r['price']}$",
                callback_data=f"ap_view_{r['country_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add Country", callback_data="ap_add")])
    buttons.append([InlineKeyboardButton("🔙 Close Panel", callback_data="ap_close")])
    return InlineKeyboardMarkup(buttons)

async def admin_prices_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin command /adminprices — opens the interactive price management panel."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    text = (
        "<b>Admin Price Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\n"
        "Tap a country to edit or delete it.\n"
        "Tap ➕ Add Country to add a new one."
    )
    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )

async def ap_refresh(query, ctx):
    """Refresh the admin panel in-place."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    text = (
        "<b>Admin Price Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\n"
        "Tap a country to edit or delete it.\n"
        "Tap ➕ Add Country to add a new one."
    )
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )

async def ap_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show edit/delete options for a specific country."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_view_")[1]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM country_prices WHERE country_code=%s", (code,)
        ).fetchone()
    if not row:
        await query.answer("Not found.", show_alert=True)
        return
    flag = country_flag(code)
    await query.edit_message_text(
        f"<b>{flag} {row['country_name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Code:      <code>{row['country_code']}</code>\n"
        f"Dial code: <code>+{row['dial_code']}</code>\n"
        f"Price:     <b>{row['price']}$</b>\n\n"
        f"What would you like to do?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Price", callback_data=f"ap_edit_{code}")],
            [InlineKeyboardButton("🗑 Delete",     callback_data=f"ap_del_{code}")],
            [InlineKeyboardButton("🔙 Back",       callback_data="ap_back")],
        ])
    )

async def ap_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask admin for new price."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_edit_")[1]
    ctx.user_data["ap_edit_code"] = code
    await query.edit_message_text(
        f"✏️ Enter the new price for <code>{code}</code> (e.g. <code>1.50</code>):\n\n"
        f"Send /apcancel to go back.",
        parse_mode="HTML"
    )
    return APANEL_EDIT_WAITING

async def ap_edit_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save the new price."""
    if update.effective_user.id != ADMIN_ID:
        return
    code = ctx.user_data.get("ap_edit_code")
    try:
        price = float(update.message.text.strip().replace("$", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a number like <code>1.50</code>", parse_mode="HTML")
        return APANEL_EDIT_WAITING
    with get_db() as conn:
        conn.execute(
            "UPDATE country_prices SET price=%s, updated_at=NOW() WHERE country_code=%s",
            (price, code)
        )
    await update.message.reply_text(
        f"✅ <code>{code}</code> price updated to <b>{price}$</b>",
        parse_mode="HTML"
    )
    # Re-show the panel
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END

async def ap_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Confirm deletion."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_del_")[1]
    await query.edit_message_text(
        f"🗑 Are you sure you want to delete <code>{code}</code>?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"ap_delconfirm_{code}")],
            [InlineKeyboardButton("❌ Cancel",      callback_data=f"ap_view_{code}")],
        ])
    )

async def ap_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Execute deletion."""
    query = update.callback_query
    await query.answer()
    code = query.data.split("ap_delconfirm_")[1]
    with get_db() as conn:
        conn.execute("DELETE FROM country_prices WHERE country_code=%s", (code,))
    await query.answer(f"✅ {code} deleted.", show_alert=True)
    await ap_refresh(query, ctx)

async def ap_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ask admin for new country details."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ <b>Add New Country</b>\n\n"
        "Send the details in this format:\n"
        "<code>CODE DIALCODE PRICE Country Name</code>\n\n"
        "Example:\n"
        "<code>IN 91 2.00 India</code>\n"
        "<code>US 1 8.00 United States</code>\n\n"
        "Send /apcancel to go back.",
        parse_mode="HTML"
    )
    return APANEL_ADD_WAITING

async def ap_add_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Save the new country."""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        parts = update.message.text.strip().split(None, 3)
        code     = parts[0].upper()
        dial     = parts[1].lstrip("+")
        price    = float(parts[2])
        name     = parts[3]
        if price <= 0 or not dial.isdigit():
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Wrong format. Use:\n<code>CODE DIALCODE PRICE Country Name</code>\n"
            "Example: <code>IN 91 2.00 India</code>",
            parse_mode="HTML"
        )
        return APANEL_ADD_WAITING
    with get_db() as conn:
        conn.execute(
            "INSERT INTO country_prices (country_code, country_name, dial_code, price) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (country_code) DO UPDATE "
            "SET country_name=%s, dial_code=%s, price=%s, updated_at=NOW()",
            (code, name, dial, price, name, dial, price)
        )
    await update.message.reply_text(
        f"✅ <b>{name}</b> (<code>{code}</code>) added at <b>{price}$</b>",
        parse_mode="HTML"
    )
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END

async def ap_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Go back to the main panel list."""
    query = update.callback_query
    await query.answer()
    await ap_refresh(query, ctx)

async def ap_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Close the panel."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Price panel closed.")
    return ConversationHandler.END

async def ap_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel add/edit conversation."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, dial_code, price "
            "FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    await update.message.reply_text(
        "<b>Admin Price Panel</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total countries: <b>{len(rows)}</b>\n\nTap a country to edit or delete.",
        parse_mode="HTML",
        reply_markup=_prices_panel_keyboard(rows)
    )
    return ConversationHandler.END


# ── SELL FLOW ─────────────────────────────────────────────────────────────────
async def sell_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — shown when user taps Sell Account button."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "<b>💰 SELL ACCOUNT</b>\n"
        "<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        "📱 <b>Send your phone number</b> with country code.\n\n"
        "<b>Example:</b> <code>+919876543210</code>\n\n"
        "Check /prices to see payouts per country.\n\n"
        "Type /cancel to go back.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]
        ])
    )
    return SELL_PHONE


async def sell_get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent their phone number — send OTP via Telethon."""
    phone = update.message.text.strip()
    ctx.user_data["sell_phone"] = phone
    await update.message.reply_text("⏳ Sending OTP to your account...")

    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "❌ <b>Configuration Error</b>\n\n<code>API_ID</code> or <code>API_HASH</code> not set.",
            parse_mode="HTML")
        return ConversationHandler.END

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        ctx.user_data["sell_client"] = client
        ctx.user_data["sell_phone_code_hash"] = result.phone_code_hash
        await update.message.reply_text(
            f"<b>📩 OTP Sent!</b>\n\n"
            f"A login code was sent to <code>{phone}</code>.\n\n"
            f"Enter the OTP with spaces between each digit.\n\n"
            f"<b>Example:</b> <code>1 2 3 4 5</code>",
            parse_mode="HTML")
        return SELL_OTP
    except Exception as e:
        logger.error(f"Sell OTP error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ <b>Failed to send OTP</b>\n\n<code>{type(e).__name__}: {e}</code>\n\n"
            f"Please check the phone number and try again.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END


async def sell_get_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent OTP — sign in, save session, notify admin with approve/reject buttons."""
    # Accept both spaced "1 2 3 4 5" and plain "12345"
    otp       = update.message.text.strip().replace(" ", "")
    client    = ctx.user_data.get("sell_client")
    phone     = ctx.user_data.get("sell_phone")
    code_hash = ctx.user_data.get("sell_phone_code_hash")
    user      = update.effective_user

    if not client:
        await update.message.reply_text(
            "❌ Session expired. Please tap Sell Account again.",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    try:
        from telethon.sessions import StringSession
        await client.sign_in(phone, otp, phone_code_hash=code_hash)
        session_string = client.session.save()
        await client.disconnect()

        # Store pending sell in DB
        with get_db() as conn:
            new_acc = conn.execute(
                "INSERT INTO accounts (session, phone, price, status) VALUES (%s,%s,%s,'pending_review') RETURNING id",
                (session_string, phone, 0)
            ).fetchone()
            acc_id = new_acc["id"]

        flag, country_name = phone_to_country(phone)
        seller_name = f"@{user.username}" if user.username else user.first_name

        # Admin message with inline Approve / Reject buttons
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"sell_approve_{acc_id}"),
             InlineKeyboardButton("❌ Reject",  callback_data=f"sell_reject_{acc_id}")]
        ])
        await update.message.bot.send_message(
            ADMIN_ID,
            f"<b>📥 NEW ACCOUNT FOR SALE</b>\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"<b>🆔 Account ID:</b> <code>#{acc_id}</code>\n"
            f"<b>👤 Seller:</b> {seller_name} (<code>{user.id}</code>)\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n"
            f"<b>🌍 Country:</b> {flag} {country_name}\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"Approve to list it in the marketplace, or Reject to remove it.",
            parse_mode="HTML",
            reply_markup=admin_kb
        )

        # Channel notification
        await send_to_channel(update.message.bot, TRADES_CHANNEL,
            f"💰 <b>NEW ACCOUNT SUBMITTED FOR SALE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Account ID: <code>#{acc_id}</code>\n"
            f"🌍 Country: {flag} {country_name}\n"
            f"📱 Phone: <code>{mask_phone(phone)}</code>\n"
            f"👤 Seller: {seller_name} (<code>{user.id}</code>)\n"
            f"📊 Status: 🔄 Pending Review\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        await update.message.reply_text(
            f"<b>✅ Account Submitted!</b>\n\n"
            f"<b>📱 Phone:</b> <code>{phone}</code>\n"
            f"<b>🌍 Country:</b> {flag} {country_name}\n\n"
            f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
            f"Your account is <b>pending admin review</b>.\n"
            f"You'll be notified once it's approved and listed. <b>💰</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Sell sign-in error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ <b>Login Failed</b>\n\n<code>{e}</code>\n\n"
            f"Please try again.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard())
        return ConversationHandler.END


async def sell_approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ✅ Approve on a sell submission — asks for price."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return ConversationHandler.END
    acc_id = int(query.data.split("sell_approve_")[1])
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
    if not acc:
        await query.answer("Not found or already processed.", show_alert=True)
        return ConversationHandler.END
    ctx.user_data["sell_approve_acc_id"] = acc_id
    await query.answer()
    await query.edit_message_text(
        f"✅ Approving account <code>#{acc_id}</code>\n\n"
        f"📱 Phone: <code>{acc['phone']}</code>\n\n"
        f"Enter the listing price in USD (e.g. <code>5.00</code>):",
        parse_mode="HTML"
    )
    return SELL_APPROVE_PRICE


async def sell_approve_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sent the price after approving a sell submission."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    acc_id = ctx.user_data.get("sell_approve_acc_id")
    if not acc_id:
        await update.message.reply_text("❌ No pending approval. Use the Approve button.")
        return ConversationHandler.END
    try:
        price = float(update.message.text.strip().replace("$", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a number like <code>5.00</code>", parse_mode="HTML")
        return SELL_APPROVE_PRICE
    with get_db() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=%s", (acc_id,)).fetchone()
        if not acc:
            await update.message.reply_text(f"❌ Account #{acc_id} not found.")
            return ConversationHandler.END
        conn.execute(
            "UPDATE accounts SET price=%s, status='available' WHERE id=%s",
            (price, acc_id)
        )
    ctx.user_data.pop("sell_approve_acc_id", None)
    flag, country_name = phone_to_country(acc["phone"] or "")
    await update.message.reply_text(
        f"✅ Account <code>#{acc_id}</code> approved and listed at <b>${price:.2f}</b>",
        parse_mode="HTML"
    )
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"✅ <b>ACCOUNT APPROVED &amp; LISTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country: {flag} {country_name}\n"
        f"📱 Phone: <code>{mask_phone(acc['phone'] or '')}</code>\n"
        f"💵 Price: <b>${price:.2f}</b>\n"
        f"📊 Status: 🟢 Available\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    return ConversationHandler.END


async def sell_reject_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin taps ❌ Reject on a sell submission."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return
    acc_id = int(query.data.split("sell_reject_")[1])
    with get_db() as conn:
        acc = conn.execute(
            "SELECT * FROM accounts WHERE id=%s AND status='pending_review'", (acc_id,)
        ).fetchone()
        if not acc:
            await query.answer("Not found or already processed.", show_alert=True)
            return
        conn.execute("DELETE FROM accounts WHERE id=%s", (acc_id,))
    await query.edit_message_text(
        f"❌ Account <code>#{acc_id}</code> rejected and removed.",
        parse_mode="HTML"
    )
    await query.answer("❌ Rejected.")
    # Channel notification
    flag, country_name = phone_to_country(acc["phone"] or "")
    await send_to_channel(ctx.bot, TRADES_CHANNEL,
        f"❌ <b>ACCOUNT REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Account ID: <code>#{acc_id}</code>\n"
        f"🌍 Country: {flag} {country_name}\n"
        f"📊 Status: ❌ Rejected\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


async def sell_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel sell conversation."""
    client = ctx.user_data.get("sell_client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    await update.message.reply_text("❌ Sell cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── WALLET / REFER / WITHDRAW ─────────────────────────────────────────────────
async def show_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ensure_user(user.id, user.username or "")
    await query.edit_message_text(
        f"<b>📊 MY WALLET</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💼 Available Balance</b>\n"
        f"<b>💲 ${get_balance(user.id):.2f} USD</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>👥 Referrals:</b>       <b>{get_referral_count(user.id)}</b>\n"
        f"<b>🤝 Referral Earned:</b> <b>${get_referral_earnings(user.id):.2f}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰  Deposit",  callback_data="menu_deposit"),
             InlineKeyboardButton("💸  Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("🔙  Back to Menu", callback_data="menu_back")]]))

async def refer_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ref_link = f"https://t.me/{ctx.bot.username}?start=ref_{user.id}"
    await query.edit_message_text(
        f"<b>👥 REFER &amp; EARN</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"Invite friends and earn <b>{int(REFERRAL_COMMISSION*100)}% commission</b>\non every deposit — <b>forever!</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📊 Your Stats</b>\n"
        f"<b>👥 Total Referrals:</b> <b>{get_referral_count(user.id)}</b>\n"
        f"<b>💰 Total Earned:</b>    <b>${get_referral_earnings(user.id):.2f}</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>🔗 Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
        f"📤 <b>Share this link. When they deposit, you get 2% instantly!</b>",
        parse_mode="HTML", reply_markup=back_keyboard())

async def withdraw_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 — Ask for UPI ID or QR code."""
    query = update.callback_query
    await query.answer()
    balance = get_balance(query.from_user.id)
    await query.edit_message_text(
        f"<b>💸 WITHDRAW</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n\n"
        f"<b>💰 Your Balance: ${balance:.2f}</b>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>📲 Step 1 of 2</b>\n\n"
        f"Send your <b>UPI ID</b> or a <b>QR code photo</b> to receive payment.\n\n"
        f"📌 <b>UPI example:</b> <code>yourname@upi</code>\n"
        f"📌 Or send a QR code image\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"Type /cancel to go back.",
        parse_mode="HTML")
    return WITHDRAW_UPI

async def withdraw_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Got UPI/QR, now ask for amount."""
    user = update.effective_user
    ensure_user(user.id, user.username or "")

    # Accept either text (UPI ID) or photo (QR code)
    if update.message.photo:
        # Store the file_id of the largest photo
        ctx.user_data["wd_upi"] = update.message.photo[-1].file_id
        ctx.user_data["wd_upi_type"] = "qr"
        upi_display = "QR Code received ✅"
    elif update.message.text:
        upi_text = update.message.text.strip()
        ctx.user_data["wd_upi"] = upi_text
        ctx.user_data["wd_upi_type"] = "upi"
        upi_display = f"`{upi_text}`"
    else:
        await update.message.reply_text(
            "<b>❌ Please send your UPI ID as text or a QR code as a photo.</b>",
            parse_mode="HTML")
        return WITHDRAW_UPI

    balance = get_balance(user.id)
    await update.message.reply_text(
        f"<b>✅ Payment details received:</b> {upi_display}\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>💰 Step 2 of 2</b>\n\n"
        f"<b>Your current balance: ${balance:.2f}</b>\n\n"
        f"How much do you want to withdraw?\n"
        f"📌 <b>Example:</b> <code>10</code>\n\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"Type /cancel to go back.",
        parse_mode="HTML")
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Got amount, validate balance and submit request."""
    user = update.effective_user
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "<b>❌ Invalid amount.</b> Enter a positive number like <code>10</code>.",
            parse_mode="HTML")
        return WITHDRAW_AMOUNT

    balance = get_balance(user.id)
    if balance < amount:
        await update.message.reply_text(
            f"<b>❌ Insufficient Balance</b>\n\n"
            f"<b>💰 Your balance: ${balance:.2f}</b>\n"
            f"<b>💸 Requested:    ${amount:.2f}</b>\n\n"
            f"You can only withdraw up to <b>${balance:.2f}</b>.\n"
            f"Please enter a lower amount:",
            parse_mode="HTML")
        return WITHDRAW_AMOUNT

    upi_val  = ctx.user_data.get("wd_upi", "")
    upi_type = ctx.user_data.get("wd_upi_type", "upi")

    # Deduct balance and record withdrawal
    with get_db() as conn:
        wd_id = conn.execute(
            "INSERT INTO withdrawals (user_id, amount, upi_id) VALUES (%s,%s,%s) RETURNING id",
            (user.id, amount, upi_val if upi_type == "upi" else "[QR Code]")
        ).fetchone()["id"]
        conn.execute(
            "UPDATE users SET balance=balance-%s WHERE user_id=%s",
            (amount, user.id)
        )

    new_balance = get_balance(user.id)

    await update.message.reply_text(
        f"<b>✅ Withdrawal Request Submitted!</b>\n\n"
        f"<b>💸 Amount: ${amount:.2f}</b>\n"
        f"<b>🆔 Reference ID:</b> <code>{wd_id}</code>\n"
        f"<b>💰 Remaining Balance: ${new_balance:.2f}</b>\n\n"
        f"⏳ <b>Admin will review and process your withdrawal shortly.</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())

    # Inline buttons for admin
    wd_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}"),
         InlineKeyboardButton("❌ Reject",  callback_data=f"wd_reject_{wd_id}")]
    ])

    upi_line = (
        f"<b>📲 UPI ID:</b> <code>{upi_val}</code>" if upi_type == "upi"
        else "<b>📲 Payment:</b> QR Code (see below)"
    )

    admin_text = (
        f"<b>💸 NEW WITHDRAWAL REQUEST</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>👤 User:</b> @{user.username or user.first_name} (<code>{user.id}</code>)\n"
        f"<b>💵 Amount: ${amount:.2f}</b>\n"
        f"{upi_line}\n"
        f"<b>🆔 Withdrawal ID:</b> <code>{wd_id}</code>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>"
    )

    # Channel gets NO username, NO UPI — only chat ID, amount, status
    channel_text = (
        f"<b>💸 WITHDRAWAL REQUEST</b>\n"
        f"<b>▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰</b>\n"
        f"<b>🆔 User ID:</b> <code>{user.id}</code>\n"
        f"<b>💵 Amount: ${amount:.2f}</b>\n"
        f"<b>🔖 Withdrawal ID:</b> <code>{wd_id}</code>\n"
        f"<b>📊 Status: ⏳ Pending</b>"
    )

    if upi_type == "qr":
        await ctx.bot.send_photo(ADMIN_ID, photo=upi_val, caption=admin_text,
                                 parse_mode="HTML", reply_markup=wd_kb)
    else:
        await ctx.bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=wd_kb)

    await send_to_channel(ctx.bot, FUNDS_CHANNEL, channel_text)
    return ConversationHandler.END

async def wd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin approves a withdrawal — works as both text command and inline button."""
    # Handle both callback query and text command
    if update.callback_query:
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True); return
        wd_id = int(query.data.split("_")[2])
    else:
        if update.effective_user.id != ADMIN_ID: return
        try:
            wd_id = int(update.message.text.split("_")[2])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /wd_approve_<id>"); return

    with get_db() as conn:
        wd = conn.execute("SELECT * FROM withdrawals WHERE id=%s", (wd_id,)).fetchone()
        if not wd:
            if update.callback_query: await update.callback_query.answer("Not found.", show_alert=True)
            else: await update.message.reply_text("❌ Not found.")
            return
        if wd["status"] != "pending":
            if update.callback_query: await update.callback_query.answer("Already processed.", show_alert=True)
            else: await update.message.reply_text("⚠️ Already processed.")
            return
        conn.execute("UPDATE withdrawals SET status='approved' WHERE id=%s", (wd_id,))

    if update.callback_query:
        await update.callback_query.edit_message_caption(
            caption=f"<b>✅ Withdrawal #{wd_id} — APPROVED</b>\n<b>💵 ${wd['amount']:.2f}</b> paid to <code>{wd['user_id']}</code>",
            parse_mode="HTML") if update.callback_query.message.caption else None
        try:
            await update.callback_query.edit_message_text(
                f"<b>✅ Withdrawal #{wd_id} — APPROVED</b>\n<b>💵 ${wd['amount']:.2f}</b> paid to <code>{wd['user_id']}</code>",
                parse_mode="HTML")
        except Exception:
            pass
        await update.callback_query.answer("✅ Approved!")
    else:
        await update.message.reply_text(
            f"✅ Withdrawal #{wd_id} approved! <b>${wd['amount']:.2f}</b> paid to <code>{wd['user_id']}</code>.",
            parse_mode="HTML")

    await ctx.bot.send_message(
        wd["user_id"],
        f"<b>🎉 Withdrawal Approved!</b>\n\n"
        f"<b>💸 ${wd['amount']:.2f}</b> has been processed.\n"
        f"<b>🆔 Ref:</b> <code>{wd_id}</code>\n\n"
        f"<b>Thank you for using TG Market! 🛒</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, FUNDS_CHANNEL,
        f"✅ <b>WITHDRAWAL APPROVED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Withdrawal ID: <code>{wd_id}</code>\n"
        f"🆔 User ID: <code>{wd['user_id']}</code>\n"
        f"💵 Amount: <b>${wd['amount']:.2f}</b>\n"
        f"📊 Status: <b>✅ Approved & Paid</b>"
    )

async def wd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin rejects a withdrawal — works as both text command and inline button."""
    if update.callback_query:
        query = update.callback_query
        if query.from_user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True); return
        wd_id = int(query.data.split("_")[2])
    else:
        if update.effective_user.id != ADMIN_ID: return
        try:
            wd_id = int(update.message.text.split("_")[2])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /wd_reject_<id>"); return

    with get_db() as conn:
        wd = conn.execute("SELECT * FROM withdrawals WHERE id=%s", (wd_id,)).fetchone()
        if not wd or wd["status"] != "pending":
            if update.callback_query: await update.callback_query.answer("Not found or already processed.", show_alert=True)
            else: await update.message.reply_text("❌ Not found or already processed.")
            return
        conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=%s", (wd_id,))
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (wd["amount"], wd["user_id"]))

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                f"<b>❌ Withdrawal #{wd_id} — REJECTED</b> — balance refunded.",
                parse_mode="HTML")
        except Exception:
            pass
        await update.callback_query.answer("❌ Rejected.")
    else:
        await update.message.reply_text(
            f"❌ Withdrawal #{wd_id} rejected. <b>${wd['amount']:.2f}</b> refunded.",
            parse_mode="HTML")

    await ctx.bot.send_message(
        wd["user_id"],
        f"<b>❌ Withdrawal Rejected</b>\n\n"
        f"Your withdrawal of <b>${wd['amount']:.2f}</b> (ID: <code>{wd_id}</code>) was not approved.\n"
        f"<b>💰 ${wd['amount']:.2f}</b> has been refunded to your balance.\n\n"
        f"Contact <b>🆘 Support</b> if this is an error.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard())
    await send_to_channel(ctx.bot, FUNDS_CHANNEL,
        f"❌ <b>WITHDRAWAL REJECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Withdrawal ID: <code>{wd_id}</code>\n"
        f"🆔 User ID: <code>{wd['user_id']}</code>\n"
        f"💵 Amount: <b>${wd['amount']:.2f}</b>\n"
        f"📊 Status: <b>❌ Rejected — Refunded</b>"
    )

# ── Admin: get file ID from a photo ──────────────────────────────────────────
async def admin_getfileid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin sends a photo → bot replies with its file_id.
    Use this to get the PAYMENT_QR_FILE_ID value."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.photo:
        await update.message.reply_text(
            "📸 Send your QR code photo directly to the bot (no command needed).\n\n"
            "Just send the image and I'll reply with the file ID.")
        return
    file_id = update.message.photo[-1].file_id
    await update.message.reply_text(
        f"✅ <b>File ID:</b>\n\n<code>{file_id}</code>\n\n"
        f"Copy this value and set it as <code>PAYMENT_QR_FILE_ID</code> in your environment variables.",
        parse_mode="HTML")


# ── Flask + Main ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_start, pattern="^menu_deposit$")],
        states={
            DEPOSIT_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            DEPOSIT_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False)
    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_menu, pattern="^menu_withdraw$")],
        states={
            WITHDRAW_UPI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi),
                MessageHandler(filters.PHOTO, withdraw_upi),
            ],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False)
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login_account", admin_login)],
        states={
            ADMIN_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            ADMIN_OTP:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp)],
            ADMIN_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False)
    sell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sell_menu, pattern="^menu_sell$")],
        states={
            SELL_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_get_phone),
            ],
            SELL_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_get_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", sell_cancel),
            CallbackQueryHandler(menu_back, pattern="^menu_back$"),
        ],
        per_message=False)
    # Admin price panel conversation
    apanel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("adminprices", admin_prices_panel),
            CallbackQueryHandler(ap_add,  pattern="^ap_add$"),
        ],
        states={
            APANEL_ADD_WAITING:  [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), ap_add_save)],
            APANEL_EDIT_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), ap_edit_save)],
        },
        fallbacks=[CommandHandler("apcancel", ap_cancel)],
        per_message=False,
        allow_reentry=True,
    )
    # Admin sell approve conversation (approve button → enter price)
    sell_approve_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sell_approve_cb, pattern=r"^sell_approve_\d+$")],
        states={
            SELL_APPROVE_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), sell_approve_price)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(deposit_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(login_conv)
    app.add_handler(sell_conv)
    app.add_handler(sell_approve_conv)
    app.add_handler(apanel_conv)
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("accounts",    admin_accounts))
    app.add_handler(CommandHandler("users",       admin_users))
    app.add_handler(CommandHandler("pending",     admin_pending))
    app.add_handler(CommandHandler("credit",      admin_credit))
    app.add_handler(CommandHandler("deduct",      admin_deduct))
    # Deposit inline approve/reject buttons
    app.add_handler(CallbackQueryHandler(dep_approve_cb, pattern=r"^dep_approve_\d+$"))
    app.add_handler(CallbackQueryHandler(dep_reject_cb,  pattern=r"^dep_reject_\d+$"))
    # Withdrawal inline approve/reject buttons
    app.add_handler(CallbackQueryHandler(wd_approve, pattern=r"^wd_approve_\d+$"))
    app.add_handler(CallbackQueryHandler(wd_reject,  pattern=r"^wd_reject_\d+$"))
    # Sell inline reject button (approve is handled by sell_approve_conv above)
    app.add_handler(CallbackQueryHandler(sell_reject_cb, pattern=r"^sell_reject_\d+$"))
    app.add_handler(CommandHandler("add_sell",    admin_add_sell))
    app.add_handler(CommandHandler("prices",      cmd_prices))
    app.add_handler(MessageHandler(filters.Regex(r"^/wd_approve_\d+$") & filters.User(ADMIN_ID), wd_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/wd_reject_\d+$")  & filters.User(ADMIN_ID), wd_reject))
    # Admin panel inline button handlers (outside conversation for view/del/back/close)
    app.add_handler(CallbackQueryHandler(ap_view,       pattern=r"^ap_view_"))
    app.add_handler(CallbackQueryHandler(ap_edit,       pattern=r"^ap_edit_"))
    app.add_handler(CallbackQueryHandler(ap_del,        pattern=r"^ap_del_[A-Z]"))
    app.add_handler(CallbackQueryHandler(ap_del_confirm,pattern=r"^ap_delconfirm_"))
    app.add_handler(CallbackQueryHandler(ap_back,       pattern="^ap_back$"))
    app.add_handler(CallbackQueryHandler(ap_close,      pattern="^ap_close$"))
    app.add_handler(MessageHandler(filters.Regex(r"^/approve_\d+$") & filters.User(ADMIN_ID), admin_approve))
    app.add_handler(MessageHandler(filters.Regex(r"^/reject_\d+$")  & filters.User(ADMIN_ID), admin_reject))
    app.add_handler(MessageHandler(filters.Regex(r"^/del_\d+$")     & filters.User(ADMIN_ID), admin_delete))
    app.add_handler(CallbackQueryHandler(buy_menu,      pattern="^menu_buy$"))
    app.add_handler(CallbackQueryHandler(buy_country,   pattern=r"^buycountry_"))
    app.add_handler(CallbackQueryHandler(view_account,  pattern=r"^view_\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_buy,   pattern=r"^confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(show_balance,  pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(refer_menu,    pattern="^menu_refer$"))
    app.add_handler(CallbackQueryHandler(menu_back,     pattern="^menu_back$"))
    return app

def main():
    global ptb_app
    init_db()
    ptb_app = build_app()

    import asyncio
    import threading

    # Create a dedicated event loop that runs in a background thread.
    # Flask runs in the main thread; all async PTB work runs on this loop.
    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    # Store loop so confirm_buy can schedule the OTP watcher on it
    flask_app.config["ASYNCIO_LOOP"] = loop

    @flask_app.get("/")
    def health():
        return Response("OK", status=200)

    @flask_app.post(f"/webhook/{BOT_TOKEN}")
    def webhook():
        data   = request.get_json(force=True)
        logger.info(f"Update {data.get('update_id')} | {list(data.keys())}")
        update = Update.de_json(data, ptb_app.bot)
        # Fire-and-forget — never block the webhook thread
        asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), loop)
        return Response("ok", status=200)

    async def setup():
        await ptb_app.initialize()
        # Clear any stale webhook/pending updates, then set fresh webhook
        await ptb_app.bot.delete_webhook(drop_pending_updates=True)
        await ptb_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}",
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"✅ Webhook set → {WEBHOOK_URL}/webhook/{BOT_TOKEN}")

    asyncio.run_coroutine_threadsafe(setup(), loop).result(timeout=30)

    logger.info(f"🚀 Starting Flask on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
