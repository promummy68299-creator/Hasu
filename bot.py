import asyncio
import html
import io
import logging
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode, KeyboardButtonStyle
from telegram.error import TelegramError, Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters,
)

# ============================================================
# CONFIGURATION — preserved from the existing project
# ============================================================
BOT_TOKEN = "8620265232:AAEHeNC2OsAmZNMCH7OV4xbhthz4uatYIho"  # Set your BotFather token here; no environment variable is used.
ADMIN_ID = 7709767483
DB_FILE = "bot.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Conversation states. No deleted/stale plan states are referenced.
PLAN_NAME, PLAN_PRICE, UPI_ID, CHANNEL_LINK, DEMO_MEDIA, WELCOME_IMAGE, WELCOME_TEXT, BROADCAST_CONTENT, REJECTION_REASON, EDIT_PLAN_NAME, EDIT_PLAN_PRICE, DB_IMPORT = range(12)

DEFAULT_WELCOME = "💎 Welcome to our Premium Service!"
LIFETIME_DAYS = 36500

# Fallback conversation routing. This keeps admin setup buttons working even if
# a deployment has an older ConversationHandler callback dispatch behavior.
def set_manual_state(context, state):
    context.user_data["_manual_state"] = state

def clear_manual_state(context):
    context.user_data.pop("_manual_state", None)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(value):
    return html.escape("" if value is None else str(value))


def bold(text):
    return f"<b>{text}</b>"


def style_button(text, callback_data=None, url=None, style=None):
    """Use official Telegram button styles supported by PTB 22.8."""
    if style is None:
        value = f"{text} {callback_data or ''}".lower()
        if any(x in value for x in ("reject", "delete", "remove", "cancel", "try again")):
            style = KeyboardButtonStyle.RED
        elif any(x in value for x in ("approve", "approved", "get premium", "join now", "choose plan", "set ", "add ", "save")):
            style = KeyboardButtonStyle.GREEN
        else:
            style = KeyboardButtonStyle.BLUE
    kwargs = {"text": text, "style": style}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    return InlineKeyboardButton(**kwargs)


def reply_button(text, style):
    return KeyboardButton(text=text, style=style)


def main_user_inline_keyboard():
    return InlineKeyboardMarkup([
        [style_button("💎 GET PREMIUM", "user:premium", style=KeyboardButtonStyle.GREEN)],
        [style_button("🥵 DEMO", "user:demo", style=KeyboardButtonStyle.RED)],
        [style_button("✅ HOW TO GET PREMIUM", "user:tutorial", style=KeyboardButtonStyle.BLUE)],
    ])


def user_reply_keyboard():
    return ReplyKeyboardMarkup(
        [
            [reply_button("💎 GET PREMIUM", KeyboardButtonStyle.GREEN)],
            [reply_button("🥵 DEMO", KeyboardButtonStyle.RED)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [style_button("💎 PLANS", "admin:plans", style=KeyboardButtonStyle.GREEN),
         style_button("🥵 DEMO", "admin:demo", style=KeyboardButtonStyle.RED)],
        [style_button("🏦 UPI SETTINGS", "admin:upi", style=KeyboardButtonStyle.BLUE),
         style_button("🖼️ WELCOME SETTINGS", "admin:welcome", style=KeyboardButtonStyle.BLUE)],
        [style_button("📢 BROADCAST", "admin:broadcast", style=KeyboardButtonStyle.BLUE)],
        [style_button("📥 IMPORT DATABASE", "admin:db:import", style=KeyboardButtonStyle.BLUE),
         style_button("📤 EXPORT DATABASE", "admin:db:export", style=KeyboardButtonStyle.BLUE)],
    ])


def back_keyboard(target="admin:menu"):
    return InlineKeyboardMarkup([[style_button("🔙 Back", target, style=KeyboardButtonStyle.BLUE)]])


def admin_only(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def safe_name(user):
    return user.full_name or user.username or str(user.id)


def init_db():
    con = db()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT NOT NULL,
            premium_until TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            validity_days INTEGER NOT NULL,
            description TEXT,
            link TEXT,
            image_file_id TEXT,
            enabled INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS plan_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS demo_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT,
            file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tutorial(
            id INTEGER PRIMARY KEY CHECK(id=1),
            video_file_id TEXT,
            text TEXT
        );

        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_id INTEGER NOT NULL,
            plan_name TEXT,
            amount REAL,
            validity INTEGER,
            screenshot_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT,
            file_id TEXT,
            text TEXT,
            created_at TEXT NOT NULL,
            total INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """)

        cols = {r["name"] for r in con.execute("PRAGMA table_info(plans)").fetchall()}
        if "category" not in cols:
            con.execute("ALTER TABLE plans ADD COLUMN category TEXT NOT NULL DEFAULT 'choose'")
        con.execute("UPDATE plans SET category='choose' WHERE category IS NULL OR category=''")

        con.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_text',?)",
            (DEFAULT_WELCOME,),
        )
        con.commit()
    finally:
        con.close()


def log_event(event, details=""):
    try:
        con = db()
        con.execute(
            "INSERT INTO logs(event,details,created_at) VALUES(?,?,?)",
            (event, str(details)[:1000], now_iso()),
        )
        con.commit()
        con.close()
    except Exception:
        logger.exception("log_event failed")


def set_setting(key, value):
    con = db()
    try:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def get_setting(key, default=None):
    con = db()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        con.close()


async def answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


def register_user(user):
    con = db()
    try:
        con.execute("""
            INSERT INTO users(id,username,first_name,last_name,registered_at,status)
            VALUES(?,?,?,?,?,'active')
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (
            user.id, user.username, user.first_name, user.last_name, now_iso()
        ))
        con.commit()
    finally:
        con.close()


async def send_start_screen(bot, chat_id):
    text = get_setting("welcome_text", DEFAULT_WELCOME) or DEFAULT_WELCOME
    image = get_setting("welcome_image")
    inline_kb = main_user_inline_keyboard()

    # Restore the original welcome inline buttons while keeping the reply keyboard
    # available separately for quick access.
    if image:
        try:
            await bot.send_photo(
                chat_id,
                image,
                caption=bold(esc(text)),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_kb,
            )
            await bot.send_message(
                chat_id,
                bold("👇 Choose an option:"),
                parse_mode=ParseMode.HTML,
                reply_markup=user_reply_keyboard(),
            )
            return
        except TelegramError:
            pass

    await bot.send_message(
        chat_id,
        bold(esc(text)),
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb,
    )
    await bot.send_message(
        chat_id,
        bold("👇 Choose an option:"),
        parse_mode=ParseMode.HTML,
        reply_markup=user_reply_keyboard(),
    )


async def tutorial_user(q, context):
    await answer(q)
    con = db()
    try:
        row = con.execute("SELECT * FROM tutorial WHERE id=1").fetchone()
    finally:
        con.close()

    if not row or (not row["video_file_id"] and not row["text"]):
        await q.message.reply_text(
            bold("✅ How to Get Premium is not configured yet."),
            parse_mode=ParseMode.HTML,
        )
        return

    if row["video_file_id"]:
        try:
            await context.bot.send_video(q.message.chat_id, row["video_file_id"])
        except TelegramError:
            pass
    if row["text"]:
        await context.bot.send_message(
            q.message.chat_id,
            bold(esc(row["text"])),
            parse_mode=ParseMode.HTML,
        )
    await context.bot.send_message(
        q.message.chat_id,
        bold("✅ HOW TO GET PREMIUM"),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    register_user(update.effective_user)
    await send_start_screen(context.bot, update.effective_chat.id)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text(bold("⛔ Admin access required"), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        bold("🔐 Admin Panel"),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def edit_or_send(q, text, keyboard=None):
    try:
        await q.edit_message_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except TelegramError:
        await q.message.reply_text(
            bold(text), parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


def plan_heading(name):
    value = (name or "").strip().upper()
    return value if value.endswith(" PLAN") else f"{value} PLAN"


def get_plans(category):
    con = db()
    try:
        return con.execute(
            "SELECT * FROM plans WHERE enabled=1 AND category=? ORDER BY id",
            (category,),
        ).fetchall()
    finally:
        con.close()


async def send_category_plans(bot, chat_id, category="choose", admin_view=False):
    # CHOOSE and PRO are separate categories. The heading is shown once and
    # each plan is one full-width button on its own row.
    heading = (
        "══════« PRO PLAN »═════"
        if category == "pro"
        else "══════« CHOSE PLAN ✅»═════"
    )

    plans = get_plans(category)

    if not plans:
        await bot.send_message(
            chat_id,
            bold(heading + "\n\nNo plans available."),
            parse_mode=ParseMode.HTML,
        )
        if admin_view:
            await bot.send_message(
                chat_id,
                bold("Use the buttons below to add a plan."),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [style_button("➕ ADD CHOOSE PLAN", "admin:plans:add:choose", style=KeyboardButtonStyle.GREEN)],
                    [style_button("➕ ADD PRO PLAN", "admin:plans:add:pro", style=KeyboardButtonStyle.GREEN)],
                ]),
            )
        return

    rows = []
    for plan in plans:
        plan_button = style_button(
            f"💎 {plan['name']}",
            f"user:plan:{plan['id']}",
            style=KeyboardButtonStyle.GREEN,
        )
        if admin_view:
            rows.append([
                plan_button,
                style_button(
                    "🗑️ DELETE",
                    f"admin:plan:delete:{plan['id']}",
                    style=KeyboardButtonStyle.RED,
                ),
            ])
        else:
            rows.append([plan_button])

    # One category heading + all plans beneath it. No per-plan heading,
    # price, validity or extra user buttons.
    await bot.send_message(
        chat_id,
        bold(heading),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )

    if admin_view:
        await bot.send_message(
            chat_id,
            bold("Plan controls"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [style_button("➕ ADD CHOOSE PLAN", "admin:plans:add:choose", style=KeyboardButtonStyle.GREEN)],
                [style_button("➕ ADD PRO PLAN", "admin:plans:add:pro", style=KeyboardButtonStyle.GREEN)],
                [style_button("🔙 Back", "admin:plans", style=KeyboardButtonStyle.BLUE)],
            ]),
        )

async def show_choose_plans(q):
    await answer(q)
    await send_category_plans(q.get_bot(), q.message.chat_id, "choose")


async def send_pro_plans(q):
    await answer(q)
    await send_category_plans(q.get_bot(), q.message.chat_id, "pro")


async def send_plan(q, context, plan_id):
    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1",
            (plan_id,),
        ).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan unavailable.", True)
        return

    upi = (get_setting("upi_id") or "").strip()
    if not upi:
        await answer(q)
        await q.message.reply_text(
            bold("🏦 UPI payment is not configured yet."),
            parse_mode=ParseMode.HTML,
        )
        return

    price = float(p["price"])
    amount = f"{price:.2f}".rstrip("0").rstrip(".")
    merchant = "Premium"
    uri = "upi://pay?" + urllib.parse.urlencode({
        "pa": upi,
        "pn": merchant,
        "am": amount,
        "cu": "INR",
    })

    qr = io.BytesIO()
    qrcode.make(uri).save(qr, format="PNG")
    qr.seek(0)

    caption = (
        f"🏷️ Price : ₹{price:g}\n\n"
        f"🏦 𝐔𝐏𝐈 𝐈𝐃: {esc(upi)}\n\n"
        "1️⃣ 𝐒𝐜𝐚𝐧  |  2️⃣ 𝐏𝐚𝐲  |  3️⃣ 𝐂𝐥𝐢𝐜𝐤 ' GET LINK '"
    )

    keyboard = InlineKeyboardMarkup([[
        style_button("GET LINK", f"user:getlink:{p['id']}", style=KeyboardButtonStyle.GREEN)
    ]])

    await answer(q)
    await context.bot.send_photo(
        q.message.chat_id,
        qr,
        caption=bold(caption),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def get_link(q, context, plan_id):
    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)
        ).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan unavailable.", True)
        return

    context.user_data["payment_plan_id"] = p["id"]
    context.user_data["awaiting_payment_screenshot"] = True
    await answer(q)
    await context.bot.send_message(
        q.message.chat_id,
        bold("📸 Please send your payment screenshot."),
        parse_mode=ParseMode.HTML,
    )


async def handle_payment_photo(update, context):
    if not context.user_data.get("awaiting_payment_screenshot"):
        return False

    if not update.message or not update.message.photo:
        return False

    plan_id = context.user_data.get("payment_plan_id")
    user = update.effective_user

    con = db()
    try:
        p = con.execute(
            "SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)
        ).fetchone()

        if not p:
            context.user_data.pop("payment_plan_id", None)
            context.user_data.pop("awaiting_payment_screenshot", None)
            await update.message.reply_text(
                bold("❌ The selected plan is no longer available."),
                parse_mode=ParseMode.HTML,
            )
            return True

        cur = con.execute("""
            INSERT INTO payments(
                user_id,username,plan_id,plan_name,amount,validity,
                screenshot_file_id,status,created_at
            )
            VALUES(?,?,?,?,?,?,?,'pending',?)
        """, (
            user.id,
            user.username or "",
            p["id"],
            p["name"],
            p["price"],
            p["validity_days"],
            update.message.photo[-1].file_id,
            now_iso(),
        ))
        payment_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    context.user_data.pop("payment_plan_id", None)
    context.user_data.pop("awaiting_payment_screenshot", None)

    admin_text = (
        "💳 PAYMENT REQUEST\n\n"
        f"PLAN NAME: {esc(p['name'])}\n"
        f"USER: {esc(safe_name(user))}\n"
        f"USER ID: {user.id}\n"
        f"PRICE: ₹{p['price']:g}\n"
        f"PAYMENT ID: {payment_id}"
    )

    keyboard = InlineKeyboardMarkup([[
        style_button(
            "✅ APPROVED",
            f"admin:payment:approve:{payment_id}",
            style=KeyboardButtonStyle.GREEN,
        ),
        style_button(
            "❌ REJECT",
            f"admin:payment:reject:{payment_id}",
            style=KeyboardButtonStyle.RED,
        ),
    ]])

    try:
        await context.bot.send_photo(
            ADMIN_ID,
            update.message.photo[-1].file_id,
            caption=bold(admin_text),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        log_event("Admin payment notification failed", exc)

    await update.message.reply_text(
        bold("✅ Payment screenshot received."),
        parse_mode=ParseMode.HTML,
    )
    return True


async def approve_payment(q, context, payment_id):
    if not admin_only(q):
        return

    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        payment = con.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()

        if not payment:
            con.rollback()
            await answer(q, "Payment not found.", True)
            return

        if payment["status"] != "pending":
            con.rollback()
            await answer(q, f"Payment already {payment['status']}.", True)
            return

        # Preserve the existing premium tracking while granting lifetime access.
        current = datetime.now(timezone.utc)
        user_row = con.execute(
            "SELECT premium_until FROM users WHERE id=?", (payment["user_id"],)
        ).fetchone()

        old = None
        if user_row and user_row["premium_until"]:
            try:
                old = datetime.fromisoformat(user_row["premium_until"])
            except ValueError:
                old = None

        base = old if old and old > current else current
        expiry = base + timedelta(days=LIFETIME_DAYS)

        con.execute(
            "UPDATE payments SET status='approved', approved_at=? WHERE id=?",
            (now_iso(), payment_id),
        )
        con.execute(
            "UPDATE users SET premium_until=? WHERE id=?",
            (expiry.isoformat(), payment["user_id"]),
        )
        con.commit()
    except Exception:
        con.rollback()
        logger.exception("approval failed")
        await answer(q, "Approval failed.", True)
        return
    finally:
        con.close()

    channel = (get_setting("premium_channel") or "").strip()
    join_button = None
    if channel:
        join_button = InlineKeyboardMarkup([[
            style_button(
                "JOIN NOW",
                url=channel,
                style=KeyboardButtonStyle.GREEN,
            )
        ]])

    await answer(q, "Approved")
    await context.bot.send_message(
        payment["user_id"],
        bold("YOUR PAYMENT APPROVED\n\nPREMIUM CHANNEL ACCES GRANTED"),
        parse_mode=ParseMode.HTML,
        reply_markup=join_button,
    )

    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def reject_payment_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END

    payment_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        payment = con.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
    finally:
        con.close()

    if not payment or payment["status"] != "pending":
        await answer(q, "Payment is no longer pending.", True)
        return ConversationHandler.END

    context.user_data["reject_payment_id"] = payment_id
    set_manual_state(context, REJECTION_REASON)
    await answer(q)
    await q.message.reply_text(
        bold("✍️ Send reject reason:"),
        parse_mode=ParseMode.HTML,
    )
    return REJECTION_REASON


async def process_rejection(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    payment_id = context.user_data.get("reject_payment_id")
    reason = (update.message.text or "").strip()
    if not reason:
        await update.message.reply_text(
            bold("❌ Reason cannot be empty. Send reject reason:"),
            parse_mode=ParseMode.HTML,
        )
        return REJECTION_REASON

    con = db()
    try:
        payment = con.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)
        ).fetchone()
        if not payment or payment["status"] != "pending":
            await update.message.reply_text(
                bold("❌ Payment is no longer pending."),
                parse_mode=ParseMode.HTML,
            )
            return ConversationHandler.END

        con.execute(
            "UPDATE payments SET status='rejected', rejection_reason=? WHERE id=?",
            (reason[:1000], payment_id),
        )
        con.commit()
    finally:
        con.close()

    context.user_data.pop("reject_payment_id", None)

    await update.message.reply_text(
        bold("❌ PAYMENT REJECTED"),
        parse_mode=ParseMode.HTML,
    )

    keyboard = InlineKeyboardMarkup([[
        style_button(
            "🔄 TRY AGAIN",
            f"user:retry:{payment['plan_id']}",
            style=KeyboardButtonStyle.RED,
        )
    ]])

    try:
        await context.bot.send_message(
            payment["user_id"],
            bold(f"❌ PAYMENT REJECTED\n\nReason: {esc(reason)}"),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        pass

    return ConversationHandler.END


def admin_plan_menu():
    con = db()
    try:
        plans = con.execute("SELECT * FROM plans ORDER BY CASE WHEN category='choose' THEN 0 ELSE 1 END, id").fetchall()
    finally:
        con.close()

    buttons = [
        [style_button("➕ ADD CHOOSE PLAN", "admin:plans:add:choose", style=KeyboardButtonStyle.GREEN),
         style_button("➕ ADD PRO PLAN", "admin:plans:add:pro", style=KeyboardButtonStyle.GREEN)],
    ]

    if not plans:
        return InlineKeyboardMarkup(buttons + [[style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)]])

    # Every plan gets its own edit/delete row with exactly two buttons.
    for category, label in (("choose", "══════« CHOSE PLAN »═════"), ("pro", "══════« PRO PLAN »═════")):
        category_plans = [p for p in plans if (p["category"] or "choose") == category]
        if not category_plans:
            continue
        buttons.append([style_button(label, "admin:plans", style=KeyboardButtonStyle.BLUE)])
        for p in category_plans:
            buttons.append([style_button(f"💎 {p['name']}", f"admin:plan:view:{p['id']}", style=KeyboardButtonStyle.BLUE)])
            buttons.append([
                style_button("✏️ EDIT", f"admin:plan:edit:{p['id']}", style=KeyboardButtonStyle.BLUE),
                style_button("🗑️ DELETE", f"admin:plan:delete:{p['id']}", style=KeyboardButtonStyle.RED),
            ])

    buttons.append([style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)])
    return InlineKeyboardMarkup(buttons)


async def admin_plans(q):
    await answer(q)
    await edit_or_send(
        q,
        "💎 PLANS\n\nManage Choose Plan and Pro Plan separately.",
        admin_plan_menu(),
    )


async def add_plan_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END

    category = "pro" if q.data.endswith(":pro") else "choose"
    context.user_data.clear()
    context.user_data["plan_category"] = category
    set_manual_state(context, PLAN_NAME)

    label = "CHOOSE PLAN" if category == "choose" else "PRO PLAN"
    await answer(q)
    await q.message.reply_text(
        bold(f"📝 Send Plan Name:\n\nCategory: {label}"),
        parse_mode=ParseMode.HTML,
    )
    return PLAN_NAME


async def plan_name(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text(
            bold("❌ Plan name cannot be empty.\n\n📝 Send Plan Name:"),
            parse_mode=ParseMode.HTML,
        )
        return PLAN_NAME

    context.user_data["plan_name"] = name
    set_manual_state(context, PLAN_PRICE)
    await update.message.reply_text(
        bold("💰 Send Plan Price:"),
        parse_mode=ParseMode.HTML,
    )
    return PLAN_PRICE


async def plan_price(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    try:
        price = float((update.message.text or "").strip())
        if price < 0:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(
            bold("❌ Invalid price.\n\n💰 Send Plan Price:"),
            parse_mode=ParseMode.HTML,
        )
        return PLAN_PRICE

    name = context.user_data.get("plan_name")
    category = context.user_data.get("plan_category", "choose")
    if not name:
        context.user_data.clear()
        await update.message.reply_text(
            bold("❌ Plan session expired. Please start again."),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
        return ConversationHandler.END

    con = db()
    try:
        con.execute("""
            INSERT INTO plans(
                name,price,validity_days,description,link,image_file_id,enabled,category
            )
            VALUES(?,?,?,?,?,?,1,?)
        """, (
            name,
            price,
            LIFETIME_DAYS,
            "Lifetime",
            "",
            None,
            category,
        ))
        con.commit()
    finally:
        con.close()

    context.user_data.clear()
    clear_manual_state(context)

    await update.message.reply_text(
        bold(
            "ADMIN PLAN CREATED SUCCESSFULLY\n\n"
            f"PLAN: {esc(name)}\n"
            f"PRICE: ₹{price:g}\n"
            "VALIDITY: Lifetime\n"
            f"CATEGORY: {'PRO PLAN' if category == 'pro' else 'CHOOSE PLAN'}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=admin_plan_menu(),
    )
    return ConversationHandler.END


async def edit_plan_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        con.close()
    if not plan:
        await answer(q, "Plan not found.", True)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["edit_plan_id"] = plan_id
    set_manual_state(context, EDIT_PLAN_NAME)
    await answer(q)
    await q.message.reply_text(
        bold(f"✏️ EDIT PLAN\n\nCurrent name: {esc(plan['name'])}\n\n📝 Send new Plan Name:"),
        parse_mode=ParseMode.HTML,
    )
    return EDIT_PLAN_NAME


async def edit_plan_name(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text(bold("❌ Name cannot be empty.\n\n📝 Send new Plan Name:"), parse_mode=ParseMode.HTML)
        return EDIT_PLAN_NAME
    context.user_data["edit_plan_name"] = name
    set_manual_state(context, EDIT_PLAN_PRICE)
    await update.message.reply_text(bold("💰 Send new Plan Price:"), parse_mode=ParseMode.HTML)
    return EDIT_PLAN_PRICE


async def edit_plan_price(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    try:
        price = float((update.message.text or "").strip())
        if price < 0:
            raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text(bold("❌ Invalid price.\n\n💰 Send new Plan Price:"), parse_mode=ParseMode.HTML)
        return EDIT_PLAN_PRICE
    plan_id = context.user_data.get("edit_plan_id")
    name = context.user_data.get("edit_plan_name")
    if not plan_id or not name:
        context.user_data.clear()
        await update.message.reply_text(bold("❌ Edit session expired."), parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
        return ConversationHandler.END
    con = db()
    try:
        cur = con.execute("UPDATE plans SET name=?, price=?, validity_days=?, description=? WHERE id=?",
                          (name, price, LIFETIME_DAYS, "Lifetime", plan_id))
        con.commit()
        if cur.rowcount == 0:
            await update.message.reply_text(bold("❌ Plan not found."), parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
            return ConversationHandler.END
    finally:
        con.close()
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold(f"✅ PLAN UPDATED SUCCESSFULLY\n\nPLAN: {esc(name)}\nPRICE: ₹{price:g}\nVALIDITY: Lifetime"),
        parse_mode=ParseMode.HTML, reply_markup=admin_plan_menu())
    return ConversationHandler.END


async def delete_plan(q, context):
    if not admin_only(q):
        return

    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            await answer(q, "Plan not found.", True)
            return
        con.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        con.execute("DELETE FROM plan_media WHERE plan_id=?", (plan_id,))
        con.commit()
    finally:
        con.close()

    await answer(q, "Plan deleted")
    await edit_or_send(q, "💎 PLANS", admin_plan_menu())


async def view_plan(q, context):
    plan_id = int(q.data.rsplit(":", 1)[1])
    con = db()
    try:
        p = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        con.close()

    if not p:
        await answer(q, "Plan not found.", True)
        return

    await answer(q)
    await edit_or_send(
        q,
        f"💎 {esc(p['name'])}\n\n"
        f"CATEGORY: {'PRO PLAN' if p['category']=='pro' else 'CHOOSE PLAN'}\n"
        f"PRICE: ₹{p['price']:g}\n"
        "VALIDITY: Lifetime",
        InlineKeyboardMarkup([[
            style_button(
                "🗑️ DELETE",
                f"admin:plan:delete:{p['id']}",
                style=KeyboardButtonStyle.RED,
            ),
            style_button("🔙 Back", "admin:plans", style=KeyboardButtonStyle.BLUE),
        ]]),
    )


def upi_settings_keyboard():
    return InlineKeyboardMarkup([
        [style_button("➕ SET UPI ID", "admin:upi:set", style=KeyboardButtonStyle.GREEN)],
        [style_button("🔗 SET PREMIUM CHANNEL", "admin:channel:set", style=KeyboardButtonStyle.GREEN)],
        [style_button("📋 VIEW PAYMENT SETTINGS", "admin:upi:view", style=KeyboardButtonStyle.BLUE)],
        [style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)],
    ])


async def upi_menu(q):
    await answer(q)
    await edit_or_send(q, "🏦 UPI SETTINGS", upi_settings_keyboard())


async def upi_start(q, context):
    context.user_data.clear()
    set_manual_state(context, UPI_ID)
    await answer(q)
    await q.message.reply_text(
        bold("🏦 Send UPI ID:"),
        parse_mode=ParseMode.HTML,
    )
    return UPI_ID


async def upi_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    if "@" not in value or len(value) > 200:
        await update.message.reply_text(
            bold("❌ Invalid UPI ID.\n\n🏦 Send UPI ID:"),
            parse_mode=ParseMode.HTML,
        )
        return UPI_ID

    set_setting("upi_id", value)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ UPI ID saved successfully."),
        parse_mode=ParseMode.HTML,
        reply_markup=upi_settings_keyboard(),
    )
    return ConversationHandler.END


async def channel_start(q, context):
    context.user_data.clear()
    set_manual_state(context, CHANNEL_LINK)
    await answer(q)
    await q.message.reply_text(
        bold("🔗 Send Premium Channel invite/link:"),
        parse_mode=ParseMode.HTML,
    )
    return CHANNEL_LINK


async def channel_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    if not value.startswith(("https://", "http://", "tg://")):
        await update.message.reply_text(
            bold("❌ Invalid channel link.\n\n🔗 Send Premium Channel invite/link:"),
            parse_mode=ParseMode.HTML,
        )
        return CHANNEL_LINK

    set_setting("premium_channel", value)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Premium Channel link saved successfully."),
        parse_mode=ParseMode.HTML,
        reply_markup=upi_settings_keyboard(),
    )
    return ConversationHandler.END


async def upi_view(q):
    upi = get_setting("upi_id", "Not configured") or "Not configured"
    channel = get_setting("premium_channel", "Not configured") or "Not configured"
    await answer(q)
    await edit_or_send(
        q,
        f"🏦 UPI ID: {esc(upi)}\n\n🔗 Premium Channel: {esc(channel)}",
        upi_settings_keyboard(),
    )


async def demo_menu(q):
    if not admin_only(q):
        return
    await answer(q)
    await edit_or_send(
        q,
        "🥵 Demo Videos",
        InlineKeyboardMarkup([
            [style_button("➕ ADD DEMO", "admin:demo:add", style=KeyboardButtonStyle.GREEN)],
            [style_button("📋 VIEW DEMOS", "admin:demo:view", style=KeyboardButtonStyle.BLUE)],
            [style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)],
        ]),
    )


async def demo_add_start(q, context):
    context.user_data.clear()
    set_manual_state(context, DEMO_MEDIA)
    await answer(q)
    await q.message.reply_text(
        bold("🎥 Send demo video.\n\nAfter upload, the bot will confirm it was added."),
        parse_mode=ParseMode.HTML,
    )
    return DEMO_MEDIA


async def demo_add(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    media_type = None
    file_id = None

    if update.message.video:
        media_type = "video"
        file_id = update.message.video.file_id
    elif update.message.photo:
        media_type = "photo"
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        media_type = "document"
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text(
            bold("❌ Please upload a video, photo, or document."),
            parse_mode=ParseMode.HTML,
        )
        return DEMO_MEDIA

    con = db()
    try:
        next_order = con.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM demo_media"
        ).fetchone()["n"]
        con.execute(
            "INSERT INTO demo_media(media_type,file_id,sort_order) VALUES(?,?,?)",
            (media_type, file_id, next_order),
        )
        con.commit()
    finally:
        con.close()

    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Demo video added successfully."),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            style_button("🥵 DEMO", "admin:demo", style=KeyboardButtonStyle.BLUE)
        ]]),
    )
    return ConversationHandler.END


async def demo_view(q, context):
    con = db()
    try:
        rows = con.execute(
            "SELECT * FROM demo_media ORDER BY sort_order,id"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await edit_or_send(q, "🥵 No demo media found.", back_keyboard("admin:demo"))
        return

    await answer(q)
    for row in rows:
        try:
            if row["media_type"] == "video":
                await context.bot.send_video(q.message.chat_id, row["file_id"])
            elif row["media_type"] == "photo":
                await context.bot.send_photo(q.message.chat_id, row["file_id"])
            else:
                await context.bot.send_document(q.message.chat_id, row["file_id"])
        except TelegramError:
            pass

    await q.message.reply_text(
        bold("🥵 DEMO"),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:demo"),
    )


async def welcome_menu(q):
    await answer(q)
    await edit_or_send(
        q,
        "🖼️ Welcome Settings",
        InlineKeyboardMarkup([
            [style_button("🖼️ SET WELCOME IMAGE", "admin:welcome:image", style=KeyboardButtonStyle.BLUE)],
            [style_button("✏️ SET WELCOME TEXT", "admin:welcome:text", style=KeyboardButtonStyle.BLUE)],
            [style_button("👁️ PREVIEW", "admin:welcome:preview", style=KeyboardButtonStyle.BLUE)],
            [style_button("🗑️ REMOVE IMAGE", "admin:welcome:remove", style=KeyboardButtonStyle.RED)],
            [style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)],
        ]),
    )


async def welcome_image_start(q, context):
    context.user_data.clear()
    set_manual_state(context, WELCOME_IMAGE)
    await answer(q)
    await q.message.reply_text(
        bold("🖼️ Send welcome image:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_IMAGE


async def welcome_image_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text(
            bold("❌ Send an image."),
            parse_mode=ParseMode.HTML,
        )
        return WELCOME_IMAGE

    set_setting("welcome_image", update.message.photo[-1].file_id)
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Welcome image saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_text_start(q, context):
    context.user_data.clear()
    set_manual_state(context, WELCOME_TEXT)
    await answer(q)
    await q.message.reply_text(
        bold("✏️ Send welcome text:"),
        parse_mode=ParseMode.HTML,
    )
    return WELCOME_TEXT


async def welcome_text_save(update, context):
    if not admin_only(update):
        return ConversationHandler.END
    set_setting("welcome_text", update.message.text or "")
    context.user_data.clear()
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Welcome text saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:welcome"),
    )
    return ConversationHandler.END


async def welcome_preview(q, context):
    await answer(q)
    await send_start_screen(context.bot, q.message.chat_id)


async def welcome_remove(q):
    set_setting("welcome_image", "")
    await answer(q)
    await edit_or_send(q, "🗑️ Welcome image removed.", back_keyboard("admin:welcome"))


async def broadcast_menu(q):
    await answer(q)
    await edit_or_send(
        q,
        "📢 Broadcast",
        InlineKeyboardMarkup([
            [style_button("📝 CREATE BROADCAST", "admin:broadcast:create", style=KeyboardButtonStyle.GREEN)],
            [style_button("🔙 Back", "admin:menu", style=KeyboardButtonStyle.BLUE)],
        ]),
    )


async def broadcast_start(q, context):
    context.user_data.clear()
    set_manual_state(context, BROADCAST_CONTENT)
    await answer(q)
    await q.message.reply_text(
        bold("📝 Send broadcast content: text, photo, video, document, audio, voice, animation, or sticker."),
        parse_mode=ParseMode.HTML,
    )
    return BROADCAST_CONTENT


def extract_broadcast(message):
    if message.text:
        return "text", None, message.text
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption
    if message.video:
        return "video", message.video.file_id, message.caption
    if message.document:
        return "document", message.document.file_id, message.caption
    if message.audio:
        return "audio", message.audio.file_id, message.caption
    if message.voice:
        return "voice", message.voice.file_id, message.caption
    if message.animation:
        return "animation", message.animation.file_id, message.caption
    if message.sticker:
        return "sticker", message.sticker.file_id, ""
    return None


async def broadcast_capture(update, context):
    if not admin_only(update):
        return ConversationHandler.END

    data = extract_broadcast(update.message)
    if not data:
        await update.message.reply_text(
            bold("❌ Unsupported content. Send text or supported media."),
            parse_mode=ParseMode.HTML,
        )
        return BROADCAST_CONTENT

    context.user_data["broadcast_content"] = data
    clear_manual_state(context)
    await update.message.reply_text(
        bold("✅ Broadcast content saved."),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [style_button("📢 SEND BROADCAST", "admin:broadcast:send", style=KeyboardButtonStyle.GREEN)],
            [style_button("❌ CANCEL", "admin:broadcast:cancel", style=KeyboardButtonStyle.RED)],
        ]),
    )
    return ConversationHandler.END


async def send_media(bot, chat_id, typ, file_id, text):
    caption = bold(esc(text or "")) if text else None
    kwargs = {"parse_mode": ParseMode.HTML} if caption else {}
    if typ == "photo":
        return await bot.send_photo(chat_id, file_id, caption=caption, **kwargs)
    if typ == "video":
        return await bot.send_video(chat_id, file_id, caption=caption, **kwargs)
    if typ == "document":
        return await bot.send_document(chat_id, file_id, caption=caption, **kwargs)
    if typ == "audio":
        return await bot.send_audio(chat_id, file_id, caption=caption, **kwargs)
    if typ == "voice":
        return await bot.send_voice(chat_id, file_id, caption=caption, **kwargs)
    if typ == "animation":
        return await bot.send_animation(chat_id, file_id, caption=caption, **kwargs)
    if typ == "sticker":
        return await bot.send_sticker(chat_id, file_id)
    raise ValueError("Unsupported broadcast type")


async def broadcast_send(q, context):
    if not admin_only(q):
        return

    data = context.user_data.get("broadcast_content")
    if not data:
        await answer(q, "No broadcast draft.", True)
        return

    typ, file_id, text = data
    con = db()
    try:
        users = con.execute("SELECT id FROM users").fetchall()
        cur = con.execute(
            "INSERT INTO broadcasts(content_type,file_id,text,created_at,total) VALUES(?,?,?,?,?)",
            (typ, file_id, text, now_iso(), len(users)),
        )
        broadcast_id = cur.lastrowid
        con.commit()
    finally:
        con.close()

    success = failed = 0

    for user in users:
        try:
            if typ == "text":
                await context.bot.send_message(
                    user["id"], bold(esc(text)), parse_mode=ParseMode.HTML
                )
            else:
                await send_media(context.bot, user["id"], typ, file_id, text)
            success += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                if typ == "text":
                    await context.bot.send_message(
                        user["id"], bold(esc(text)), parse_mode=ParseMode.HTML
                    )
                else:
                    await send_media(context.bot, user["id"], typ, file_id, text)
                success += 1
            except TelegramError:
                failed += 1
        except (Forbidden, TelegramError):
            failed += 1

    con = db()
    try:
        con.execute(
            "UPDATE broadcasts SET success=?,failed=? WHERE id=?",
            (success, failed, broadcast_id),
        )
        con.commit()
    finally:
        con.close()

    context.user_data.pop("broadcast_content", None)
    clear_manual_state(context)
    await answer(q, "Broadcast complete")
    await edit_or_send(
        q,
        f"📢 Broadcast Completed\n\nTotal: {len(users)}\nSuccess: {success}\nFailed: {failed}",
        back_keyboard("admin:menu"),
    )


async def export_database(q, context):
    if not admin_only(q):
        return
    await answer(q)
    tmp = DB_FILE + ".export.tmp"
    try:
        # Use SQLite's backup API so WAL data is included reliably.
        src = sqlite3.connect(DB_FILE)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp, "rb") as fh:
            data = io.BytesIO(fh.read())
        data.name = "bot.sqlite"
        await context.bot.send_document(
            q.message.chat_id, data,
            caption=bold("📤 DATABASE EXPORT"),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("database export failed")
        await q.message.reply_text(bold(f"❌ Export failed: {esc(exc)}"), parse_mode=ParseMode.HTML)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


async def import_database_start(q, context):
    if not admin_only(q):
        return ConversationHandler.END
    context.user_data.clear()
    set_manual_state(context, DB_IMPORT)
    await answer(q)
    await q.message.reply_text(
        bold("📥 IMPORT DATABASE\n\nSend the exported .sqlite or .db file now.\n\n⚠️ The imported database will replace the current database after integrity validation."),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard("admin:menu"),
    )
    return DB_IMPORT


async def import_database_file(update, context):
    if not admin_only(update) or not update.message or not update.message.document:
        return ConversationHandler.END
    document = update.message.document
    name = (document.file_name or "").lower()
    if not (name.endswith(".sqlite") or name.endswith(".db")):
        await update.message.reply_text(bold("❌ Please send a .sqlite or .db database file."), parse_mode=ParseMode.HTML)
        return DB_IMPORT

    tmp = DB_FILE + ".import.tmp"
    try:
        tg_file = await document.get_file()
        await tg_file.download_to_drive(tmp)
        con = sqlite3.connect(tmp)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if str(ok).lower() != "ok":
                raise ValueError("SQLite integrity check failed")
            required = {"users", "plans", "payments", "settings", "demo_media"}
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            missing = required - tables
            if missing:
                raise ValueError("Missing required tables: " + ", ".join(sorted(missing)))
        finally:
            con.close()
        # Keep a rollback copy until the imported DB is successfully initialized.
        backup = DB_FILE + ".before_import"
        if os.path.exists(DB_FILE):
            import shutil
            shutil.copy2(DB_FILE, backup)
        os.replace(tmp, DB_FILE)
        init_db()
        try:
            if os.path.exists(backup):
                os.remove(backup)
        except OSError:
            pass
        context.user_data.clear()
        clear_manual_state(context)
        await update.message.reply_text(
            bold("✅ DATABASE IMPORTED SUCCESSFULLY"),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
    except Exception as exc:
        logger.exception("database import failed")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        try:
            if os.path.exists(backup):
                if os.path.exists(DB_FILE):
                    os.remove(DB_FILE)
                os.replace(backup, DB_FILE)
        except OSError:
            pass
        await update.message.reply_text(bold(f"❌ Import failed: {esc(exc)}"), parse_mode=ParseMode.HTML)
        return DB_IMPORT
    return ConversationHandler.END


async def cancel_conversation(update, context):
    context.user_data.clear()
    clear_manual_state(context)
    if update.callback_query:
        await answer(update.callback_query)
    else:
        await update.message.reply_text(
            bold("❌ Cancelled."),
            parse_mode=ParseMode.HTML,
        )
    return ConversationHandler.END


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    if data.startswith("admin:") and not admin_only(update):
        await answer(q, "⛔ Admin access required", True)
        return

    try:
        if data == "user:premium":
            await show_choose_plans(q)
        elif data == "user:tutorial":
            await tutorial_user(q, context)
        elif data == "user:demo":
            await user_demo(q, context)
        elif data.startswith("user:choose"):
            await show_choose_plans(q)
        elif data.startswith("user:plan:"):
            await send_plan(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("user:getlink:"):
            await get_link(q, context, int(data.rsplit(":", 1)[1]))
        elif data.startswith("user:retry:"):
            await send_plan(q, context, int(data.rsplit(":", 1)[1]))
        elif data == "admin:menu":
            await answer(q)
            await edit_or_send(q, "🔐 Admin Panel", admin_main_keyboard())
        elif data.startswith("admin:plans:add:"):
            # Fallback for deployments where ConversationHandler does not claim the callback.
            await add_plan_start(q, context)
        elif data.startswith("admin:plan:edit:"):
            await edit_plan_start(q, context)
        elif data == "admin:db:import":
            await import_database_start(q, context)
        elif data == "admin:upi:set":
            await upi_start(q, context)
        elif data == "admin:channel:set":
            await channel_start(q, context)
        elif data == "admin:demo:add":
            await demo_add_start(q, context)
        elif data == "admin:welcome:image":
            await welcome_image_start(q, context)
        elif data == "admin:welcome:text":
            await welcome_text_start(q, context)
        elif data == "admin:broadcast:create":
            await broadcast_start(q, context)
        elif data.startswith("admin:payment:reject:"):
            await reject_payment_start(q, context)
        elif data == "admin:plans":
            await admin_plans(q)
        elif data.startswith("admin:plan:delete:"):
            await delete_plan(q, context)
        elif data.startswith("admin:plan:view:"):
            await view_plan(q, context)
        elif data == "admin:db:export":
            await export_database(q, context)
        elif data == "admin:upi":
            await upi_menu(q)
        elif data == "admin:upi:view":
            await upi_view(q)
        elif data == "admin:welcome":
            await welcome_menu(q)
        elif data == "admin:welcome:preview":
            await welcome_preview(q, context)
        elif data == "admin:welcome:remove":
            await welcome_remove(q)
        elif data == "admin:demo":
            await demo_menu(q)
        elif data == "admin:demo:view":
            await demo_view(q, context)
        elif data == "admin:broadcast":
            await broadcast_menu(q)
        elif data == "admin:broadcast:send":
            await broadcast_send(q, context)
        elif data == "admin:broadcast:cancel":
            context.user_data.pop("broadcast_content", None)
            await answer(q)
            await edit_or_send(q, "❌ Broadcast cancelled.", back_keyboard("admin:broadcast"))
        elif data.startswith("admin:payment:approve:"):
            await approve_payment(q, context, int(data.rsplit(":", 1)[1]))
        else:
            await answer(q, "Invalid or expired action.", True)
    except Exception as exc:
        logger.exception("callback error")
        log_event("callback error", exc)
        await answer(q, "❌ Something went wrong.", True)


async def user_demo(q, context):
    await answer(q)

    con = db()
    try:
        rows = con.execute(
            "SELECT * FROM demo_media ORDER BY sort_order,id"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        await context.bot.send_message(
            q.message.chat_id,
            bold("🥵 No demo videos are configured yet."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                style_button("💎 CHOOSE PLAN", "user:premium", style=KeyboardButtonStyle.GREEN)
            ]]),
        )
        return

    for row in rows:
        try:
            if row["media_type"] == "video":
                await context.bot.send_video(q.message.chat_id, row["file_id"])
            elif row["media_type"] == "photo":
                await context.bot.send_photo(q.message.chat_id, row["file_id"])
            else:
                await context.bot.send_document(q.message.chat_id, row["file_id"])
        except TelegramError:
            pass

    await context.bot.send_message(
        q.message.chat_id,
        bold("🥵 DEMO"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            style_button("💎 CHOOSE PLAN", "user:premium", style=KeyboardButtonStyle.GREEN)
        ]]),
    )


async def message_router(update, context):
    if not update.message:
        return

    # Reliable fallback for admin setup flows when ConversationHandler is not active.
    manual = context.user_data.get("_manual_state")
    if admin_only(update) and manual is not None:
        if manual == PLAN_NAME:
            result = await plan_name(update, context)
            return result
        if manual == PLAN_PRICE:
            result = await plan_price(update, context)
            return result
        if manual == EDIT_PLAN_NAME:
            result = await edit_plan_name(update, context)
            return result
        if manual == EDIT_PLAN_PRICE:
            result = await edit_plan_price(update, context)
            return result
        if manual == UPI_ID:
            result = await upi_save(update, context)
            return result
        if manual == CHANNEL_LINK:
            result = await channel_save(update, context)
            return result
        if manual == DEMO_MEDIA:
            result = await demo_add(update, context)
            return result
        if manual == WELCOME_IMAGE:
            result = await welcome_image_save(update, context)
            return result
        if manual == WELCOME_TEXT:
            result = await welcome_text_save(update, context)
            return result
        if manual == BROADCAST_CONTENT:
            result = await broadcast_capture(update, context)
            return result
        if manual == REJECTION_REASON:
            result = await process_rejection(update, context)
            return result
        if manual == DB_IMPORT:
            result = await import_database_file(update, context)
            return result

    # Reply-keyboard buttons are normal messages, so handle them explicitly.
    text = (update.message.text or "").strip()
    if text == "💎 GET PREMIUM":
        register_user(update.effective_user)
        await send_category_plans(context.bot, update.effective_chat.id, "choose")
        return

    if text == "🥵 DEMO":
        register_user(update.effective_user)
        # Reuse the exact same new-message demo flow without editing the welcome message.
        con = db()
        try:
            rows = con.execute(
                "SELECT * FROM demo_media ORDER BY sort_order,id"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            await context.bot.send_message(
                update.effective_chat.id,
                bold("🥵 No demo videos are configured yet."),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    style_button("💎 CHOOSE PLAN", "user:premium", style=KeyboardButtonStyle.GREEN)
                ]]),
            )
            return

        for row in rows:
            try:
                if row["media_type"] == "video":
                    await context.bot.send_video(update.effective_chat.id, row["file_id"])
                elif row["media_type"] == "photo":
                    await context.bot.send_photo(update.effective_chat.id, row["file_id"])
                else:
                    await context.bot.send_document(update.effective_chat.id, row["file_id"])
            except TelegramError:
                pass

        await context.bot.send_message(
            update.effective_chat.id,
            bold("🥵 DEMO"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                style_button("💎 CHOOSE PLAN", "user:premium", style=KeyboardButtonStyle.GREEN)
            ]]),
        )


async def post_init(app):
    init_db()
    log_event("Bot Started")
    try:
        me = await app.bot.get_me()
        logger.info("Bot started as @%s", me.username)
    except TelegramError as exc:
        log_event("Telegram API error", exc)


def build_app():

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Command handlers MUST be registered before the catch-all message router.
    # The previous build accidentally omitted /start and /admin, so Telegram
    # accepted updates but the bot never produced a response.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))

    # All callbacks are handled centrally. Admin input flows use the
    # explicit _manual_state router below, which avoids ConversationHandler
    # entry-point/state conflicts on deployments and keeps every admin button
    # deterministic.
    app.add_handler(CallbackQueryHandler(callback))

    # Admin setup flows must accept the actual content type requested by the
    # current state (text, photo, document, video, etc.). Put this before the
    # payment screenshot handler so admin media is never misrouted.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router), group=0)
    # User payment screenshots.
    app.add_handler(MessageHandler(filters.PHOTO, handle_payment_photo), group=5)

    return app


if __name__ == "__main__":
    init_db()
    application = build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
