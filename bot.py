import asyncio
import html
import io
import logging
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import qrcode
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.constants import ParseMode, KeyboardButtonStyle
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

# ============================================================
# CONFIGURATION
# ============================================================
BOT_TOKEN = "8620265232:AAGTIMdz_LoV_EPP8FN_7rMeaizqtwO-API"
ADMIN_ID = 7709767483
DB_FILE = "bot.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Conversation states
(
    PLAN_NAME, PLAN_PRICE, PLAN_DAYS, PLAN_DESC, PLAN_LINK, PLAN_IMAGE,
    UPI_ID,
    DEMO_MEDIA,
    TUTORIAL_VIDEO, TUTORIAL_TEXT,
    WELCOME_IMAGE, WELCOME_TEXT,
    BROADCAST_CONTENT,
    REJECTION_REASON,
) = range(14)

DEFAULT_WELCOME = "💎 Welcome to our Premium Service! Choose an option below."
db_lock = asyncio.Lock()

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    con = db()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT, last_name TEXT,
            registered_at TEXT NOT NULL, premium_until TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, price REAL NOT NULL, validity_days INTEGER NOT NULL,
            description TEXT, link TEXT, image_file_id TEXT, enabled INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS plan_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, media_type TEXT, file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS demo_media(
            id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, file_id TEXT, sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tutorial(
            id INTEGER PRIMARY KEY CHECK(id=1), video_file_id TEXT, text TEXT
        );
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, username TEXT, plan_id INTEGER NOT NULL,
            plan_name TEXT, amount REAL, validity INTEGER, screenshot_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending', rejection_reason TEXT, created_at TEXT NOT NULL,
            approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, content_type TEXT, file_id TEXT, text TEXT,
            created_at TEXT NOT NULL, total INTEGER DEFAULT 0, success INTEGER DEFAULT 0, failed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, details TEXT, created_at TEXT NOT NULL
        );
        """)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_text',?)", (DEFAULT_WELCOME,))
        con.commit()
    finally:
        con.close()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log_event(event, details=""):
    try:
        con = db()
        con.execute("INSERT INTO logs(event,details,created_at) VALUES(?,?,?)",
                    (event, details[:1000], now_iso()))
        con.commit()
        con.close()
    except Exception as e:
        logger.exception("log error: %s", e)

def esc(v):
    return html.escape("" if v is None else str(v))

def bold(text):
    return f"<b>{text}</b>"

async def safe_answer(q, text=None, show_alert=False):
    try:
        await q.answer(text, show_alert=show_alert)
    except TelegramError:
        pass

def user_registered(tg):
    con = db()
    try:
        existing = con.execute("SELECT 1 FROM users WHERE id=?", (tg.id,)).fetchone()
        con.execute("""
            INSERT INTO users(id,username,first_name,last_name,registered_at,status)
            VALUES(?,?,?,?,?,'active')
            ON CONFLICT(id) DO UPDATE SET username=excluded.username,
            first_name=excluded.first_name,last_name=excluded.last_name
        """, (tg.id, tg.username, tg.first_name, tg.last_name, now_iso()))
        con.commit()
        return existing is None
    finally:
        con.close()

def get_setting(key, default=None):
    con = db()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()

def set_setting(key, value):
    con = db()
    try:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
        con.commit()
    finally:
        con.close()

def admin_only(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

def back_kb(target="admin:menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=target)]])

def main_user_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 GET PREMIUM", callback_data="user:premium",
                              style=KeyboardButtonStyle.GREEN)],
        [InlineKeyboardButton("🥵 DEMO", callback_data="user:demo",
                              style=KeyboardButtonStyle.DANGER)],
        [InlineKeyboardButton("✅ HOW TO GET PREMIUM", callback_data="user:tutorial",
                              style=KeyboardButtonStyle.BLUE)]
    ])

def main_reply_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💎 GET PREMIUM")],
            [KeyboardButton("🥵 DEMO")],
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def set_flow_state(context, state):
    context.user_data["_flow_state"] = state
    return state

def clear_flow_state(context):
    context.user_data.pop("_flow_state", None)


async def send_start_screen(bot, chat_id):
    text = get_setting("welcome_text", DEFAULT_WELCOME) or DEFAULT_WELCOME
    image = get_setting("welcome_image")
    if image:
        try:
            await bot.send_photo(chat_id, image, caption=bold(esc(text)), parse_mode=ParseMode.HTML,
                                 reply_markup=main_user_kb())
            await bot.send_message(chat_id, bold("👇 Choose an option:"), parse_mode=ParseMode.HTML,
                                   reply_markup=main_reply_kb())
            return
        except TelegramError:
            pass
    await bot.send_message(chat_id, bold(esc(text)), parse_mode=ParseMode.HTML,
                           reply_markup=main_user_kb())
    await bot.send_message(chat_id, bold("👇 Choose an option:"), parse_mode=ParseMode.HTML,
                           reply_markup=main_reply_kb())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    new = user_registered(update.effective_user)
    log_event("/start", str(update.effective_user.id))
    if new:
        log_event("User Registered", str(update.effective_user.id))
    await send_start_screen(context.bot, update.effective_chat.id)

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Premium Plans", callback_data="admin:plans")],
        [InlineKeyboardButton("🥵 Demo Videos", callback_data="admin:demo")],
        [InlineKeyboardButton("💳 Get Premium", callback_data="admin:getpremium")],
        [InlineKeyboardButton("🖼️ Welcome Settings", callback_data="admin:welcome")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast")]
    ])

async def admin_cmd(update, context):
    if not admin_only(update):
        await update.message.reply_text(bold("❌ Access Denied"), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(bold("🔐 Admin Panel"), parse_mode=ParseMode.HTML, reply_markup=admin_menu_kb())

async def edit_or_send(q, text, kb=None):
    try:
        await q.edit_message_text(bold(text), parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest:
        await q.message.reply_text(bold(text), parse_mode=ParseMode.HTML, reply_markup=kb)

async def show_plans(q):
    con = db()
    rows = con.execute("SELECT * FROM plans WHERE enabled=1 ORDER BY id").fetchall()
    con.close()
    if not rows:
        await edit_or_send(q, "💎 No premium plans are currently available.", back_kb("user:back"))
        return
    buttons = []
    for p in rows:
        buttons.append([InlineKeyboardButton(
            f"💎 {p['name']} — ₹{p['price']:g}",
            callback_data=f"user:plan:{p['id']}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="user:back")])
    await edit_or_send(q, "💎 Premium Plans\n\n" + "\n\n".join(
        f"<b>{esc(p['name'])}</b>\n💰 ₹{p['price']:g}\n⏳ {p['validity_days']} Days\n📝 {esc(p['description'] or '')}"
        for p in rows), InlineKeyboardMarkup(buttons))

async def send_plan(q, context, plan_id):
    con = db()
    p = con.execute("SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)).fetchone()
    con.close()
    if not p:
        await safe_answer(q, "Plan unavailable.", True); return
    log_event("Plan Selected", f"{q.from_user.id}:{plan_id}")
    upi = get_setting("upi_id")
    if not upi:
        await edit_or_send(q, "💳 Payment is not configured yet. Please contact the administrator.",
                           back_kb("user:premium"))
        return
    amount = f"{p['price']:.2f}".rstrip("0").rstrip(".")
    uri = "upi://pay?" + urllib.parse.urlencode({"pa": upi, "pn": "Premium", "am": amount, "cu": "INR"})
    qr = None
    try:
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG"); buf.seek(0); qr = buf
        log_event("QR Generated", f"{q.from_user.id}:{plan_id}")
    except Exception as e:
        log_event("Telegram API Errors", f"QR generation: {e}")
    caption = (
        f"💎 Premium Payment\n\n📦 Plan: {esc(p['name'])}\n💰 Price: ₹{p['price']:g}\n"
        f"⏳ Validity: {p['validity_days']} Days\n💳 UPI ID: {esc(upi)}\n\n"
        "Pay the exact amount and then tap I Have Paid."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 I Have Paid", callback_data=f"user:payment:paid:{p['id']}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="user:back")],
        [InlineKeyboardButton("🔙 Back", callback_data="user:premium")]
    ])
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if qr:
        await context.bot.send_photo(q.message.chat_id, qr, caption=bold(caption), parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await context.bot.send_message(q.message.chat_id, bold(caption), parse_mode=ParseMode.HTML, reply_markup=kb)

async def demo_user(q, context):
    con = db()
    rows = con.execute("SELECT * FROM demo_media ORDER BY sort_order,id").fetchall()
    con.close()
    if not rows:
        await edit_or_send(q, "🥵 No demo videos are configured yet.", back_kb("user:back")); return
    await safe_answer(q)
    for r in rows:
        try:
            if r["media_type"] == "video":
                await context.bot.send_video(q.message.chat_id, r["file_id"])
            else:
                await context.bot.send_photo(q.message.chat_id, r["file_id"])
        except TelegramError:
            continue
    await context.bot.send_message(q.message.chat_id, bold("🥵 Demo Videos"), parse_mode=ParseMode.HTML, reply_markup=back_kb("user:back"))

async def tutorial_user(q, context):
    con = db()
    r = con.execute("SELECT * FROM tutorial WHERE id=1").fetchone()
    con.close()
    if not r:
        await edit_or_send(q, "✅ Tutorial is not configured yet.", back_kb("user:back")); return
    await safe_answer(q)
    if r["video_file_id"]:
        try: await context.bot.send_video(q.message.chat_id, r["video_file_id"])
        except TelegramError: pass
    if r["text"]:
        await context.bot.send_message(q.message.chat_id, bold(esc(r["text"])), parse_mode=ParseMode.HTML)
    await context.bot.send_message(q.message.chat_id, bold("✅ How to Get Premium"), parse_mode=ParseMode.HTML, reply_markup=back_kb("user:back"))

def payment_pending_kb(pid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📸 I Have Paid", callback_data=f"user:payment:paid:{pid}")],
                                 [InlineKeyboardButton("❌ Cancel", callback_data="user:back")]])

async def request_payment(update, context, plan_id):
    user = update.effective_user
    con = db()
    p = con.execute("SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)).fetchone()
    con.close()
    if not p:
        await safe_answer(update.callback_query, "Plan unavailable.", True); return
    context.user_data["payment_plan_id"] = plan_id
    context.user_data["awaiting_payment_screenshot"] = True
    await update.callback_query.message.reply_text(
        bold(f"📸 Please send your payment screenshot for <b>{esc(p['name'])}</b> (₹{p['price']:g})."),
        parse_mode=ParseMode.HTML, reply_markup=payment_pending_kb(plan_id))

async def handle_payment_photo(update, context):
    if not context.user_data.get("awaiting_payment_screenshot"):
        return False
    user = update.effective_user
    photos = update.message.photo
    if not photos:
        return False
    plan_id = context.user_data.get("payment_plan_id")
    con = db()
    p = con.execute("SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,)).fetchone()
    if not p:
        con.close(); context.user_data.clear()
        await update.message.reply_text(bold("❌ Plan is no longer available."), parse_mode=ParseMode.HTML); return True
    cur = con.execute("""
        INSERT INTO payments(user_id,username,plan_id,plan_name,amount,validity,screenshot_file_id,status,created_at)
        VALUES(?,?,?,?,?,?,?,'pending',?)
    """, (user.id, user.username, p["id"], p["name"], p["price"], p["validity_days"], photos[-1].file_id, now_iso()))
    pid = cur.lastrowid
    con.commit(); con.close()
    context.user_data.clear()
    log_event("Payment Submitted", f"payment {pid} user {user.id}")
    caption = (f"🔔 NEW PAYMENT REQUEST\n\n👤 User: {esc(user.full_name)}\n🆔 User ID: {user.id}\n"
               f"💎 Plan: {esc(p['name'])}\n💰 Amount: ₹{p['price']:g}\n⏳ Validity: {p['validity_days']} Days\n🧾 Payment ID: {pid}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"admin:payment:approve:{pid}"),
                                InlineKeyboardButton("❌ Reject", callback_data=f"admin:payment:reject:{pid}")]])
    try:
        await context.bot.send_photo(ADMIN_ID, photos[-1].file_id, caption=bold(caption), parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        log_event("Telegram API Errors", f"admin notification: {e}")
    await update.message.reply_text(bold(f"✅ Payment submitted successfully.\n🧾 Payment ID: {pid}\nStatus: Pending"),
                                     parse_mode=ParseMode.HTML)
    return True

async def approve_payment(q, context, pid):
    if not admin_only(q): return
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p:
            con.rollback(); await safe_answer(q, "Payment not found.", True); return
        if p["status"] != "pending":
            con.rollback(); await safe_answer(q, f"Already {p['status']}.", True); return
        u = con.execute("SELECT premium_until FROM users WHERE id=?", (p["user_id"],)).fetchone()
        current = datetime.now(timezone.utc)
        old = None
        if u and u["premium_until"]:
            try: old = datetime.fromisoformat(u["premium_until"])
            except ValueError: old = None
        base = old if old and old > current else current
        expiry = base + timedelta(days=p["validity"])
        con.execute("UPDATE payments SET status='approved',approved_at=? WHERE id=?", (now_iso(), pid))
        con.execute("UPDATE users SET premium_until=? WHERE id=?", (expiry.isoformat(), p["user_id"]))
        con.commit()
    except Exception as e:
        con.rollback(); log_event("Database Errors", str(e)); await safe_answer(q, "Approval failed.", True); return
    finally:
        con.close()
    log_event("Payment Approved", f"payment {pid}")
    await safe_answer(q, "Approved")
    con = db(); plan = con.execute("SELECT link FROM plans WHERE id=?", (p["plan_id"],)).fetchone(); con.close()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join Premium", url=plan["link"])]] if plan and plan["link"] else [])
    try:
        await context.bot.send_message(p["user_id"],
            bold(f"✅ Payment Approved!\n\n💎 Plan: {esc(p['plan_name'])}\n⏳ Valid Until: {esc(expiry.strftime('%d %b %Y, %H:%M UTC'))}\n\nPremium activated successfully."),
            parse_mode=ParseMode.HTML, reply_markup=kb if kb.inline_keyboard else None)
    except TelegramError as e:
        log_event("Telegram API Errors", f"user approval message: {e}")
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except TelegramError: pass

async def reject_payment(q, context, pid):
    if not admin_only(q): return
    context.user_data["reject_pid"] = pid
    context.user_data["awaiting_rejection"] = True
    await safe_answer(q)
    await q.message.reply_text(bold("❌ Send the rejection reason, or send /skip for the default reason."),
                               parse_mode=ParseMode.HTML)

async def process_rejection(update, context):
    if not admin_only(update) or not context.user_data.get("awaiting_rejection"): return False
    pid = context.user_data.get("reject_pid")
    reason = "Please submit a valid payment proof." if update.message.text == "/skip" else update.message.text
    con = db()
    con.execute("BEGIN IMMEDIATE")
    p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] != "pending":
        con.rollback(); con.close(); context.user_data.clear()
        await update.message.reply_text(bold("❌ Payment is no longer pending."), parse_mode=ParseMode.HTML); return True
    con.execute("UPDATE payments SET status='rejected',rejection_reason=? WHERE id=?", (reason[:1000], pid))
    con.commit(); con.close(); context.user_data.clear()
    log_event("Payment Rejected", f"payment {pid}")
    await update.message.reply_text(bold("❌ Payment Rejected"), parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(p["user_id"], bold(f"❌ Payment Rejected\n\n{esc(reason)}"),
                                       parse_mode=ParseMode.HTML)
    except TelegramError: pass
    return True

def plans_admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Plan", callback_data="admin:plans:add"),
         InlineKeyboardButton("✏️ Edit Plan", callback_data="admin:plans:edit")],
        [InlineKeyboardButton("🗑️ Delete Plan", callback_data="admin:plans:delete"),
         InlineKeyboardButton("🔄 Enable / Disable", callback_data="admin:plans:toggle")],
        [InlineKeyboardButton("📋 View Plans", callback_data="admin:plans:view")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin:menu")]
    ])

async def admin_plans(q):
    await edit_or_send(q, "💎 Premium Plans", plans_admin_kb())

async def admin_getpremium(q):
    con = db()
    rows = con.execute("SELECT * FROM plans WHERE enabled=1 ORDER BY id").fetchall()
    con.close()
    if not rows:
        await edit_or_send(q, "💳 No enabled premium plans are available.", back_kb("admin:menu"))
        return
    buttons = [[InlineKeyboardButton(f"💎 {p['name']} — ₹{p['price']:g}",
                                     callback_data=f"admin:getpremium:plan:{p['id']}")]
               for p in rows]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin:menu")])
    await edit_or_send(q, "💳 Get Premium\n\n" + "\n\n".join(
        f"<b>{esc(p['name'])}</b>\n💰 ₹{p['price']:g}\n⏳ {p['validity_days']} Days\n📝 {esc(p['description'] or '')}"
        for p in rows), InlineKeyboardMarkup(buttons))

async def plan_add_start(q, context):
    context.user_data.clear(); context.user_data["plan_flow"] = "add"
    await edit_or_send(q, "➕ Send Plan Name:")
    return set_flow_state(context, PLAN_NAME)

async def plan_name(update, context):
    context.user_data["plan_name"] = update.message.text.strip()
    await update.message.reply_text(bold("💰 Send Price (numeric):"), parse_mode=ParseMode.HTML); return set_flow_state(context, PLAN_PRICE)
async def plan_price(update, context):
    try:
        v=float(update.message.text.strip())
        if v < 0: raise ValueError
    except ValueError:
        await update.message.reply_text(bold("❌ Invalid price. Send a numeric price."), parse_mode=ParseMode.HTML); return PLAN_PRICE
    context.user_data["plan_price"]=v
    await update.message.reply_text(bold("⏳ Send Validity Days (positive integer):"), parse_mode=ParseMode.HTML); return set_flow_state(context, PLAN_DAYS)
async def plan_days(update, context):
    try:
        v=int(update.message.text.strip())
        if v <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text(bold("❌ Invalid validity. Send a positive integer."), parse_mode=ParseMode.HTML); return PLAN_DAYS
    context.user_data["plan_days"]=v
    await update.message.reply_text(bold("📝 Send Description:"), parse_mode=ParseMode.HTML); return set_flow_state(context, PLAN_DESC)
async def plan_desc(update, context):
    context.user_data["plan_desc"]=update.message.text
    await update.message.reply_text(bold("🔗 Send Plan Link, or /skip:"), parse_mode=ParseMode.HTML); return set_flow_state(context, PLAN_LINK)
async def plan_link(update, context):
    context.user_data["plan_link"]="" if update.message.text=="/skip" else update.message.text.strip()
    await update.message.reply_text(bold("🖼️ Send Plan Image, or /skip:"), parse_mode=ParseMode.HTML); return set_flow_state(context, PLAN_IMAGE)
async def plan_image(update, context):
    fid = None if update.message.text=="/skip" else None
    if update.message.photo: fid=update.message.photo[-1].file_id
    elif update.message.text!="/skip":
        await update.message.reply_text(bold("❌ Send an image or /skip."), parse_mode=ParseMode.HTML); return PLAN_IMAGE
    context.user_data["plan_image"]=fid
    con=db()
    con.execute("INSERT INTO plans(name,price,validity_days,description,link,image_file_id,enabled) VALUES(?,?,?,?,?,?,1)",
                (context.user_data["plan_name"],context.user_data["plan_price"],context.user_data["plan_days"],
                 context.user_data["plan_desc"],context.user_data["plan_link"],fid))
    con.commit(); con.close()
    context.user_data.clear()
    await update.message.reply_text(bold("✅ Plan added successfully."), parse_mode=ParseMode.HTML, reply_markup=plans_admin_kb())
    clear_flow_state(context)
    return ConversationHandler.END

def cancel_flow():
    async def _cancel(update, context):
        context.user_data.clear()
        clear_flow_state(context)
        await update.message.reply_text(bold("❌ Cancelled."), parse_mode=ParseMode.HTML, reply_markup=admin_menu_kb())
        return ConversationHandler.END
    return _cancel

async def select_plan_by_action(q, action):
    con=db(); rows=con.execute("SELECT * FROM plans ORDER BY id").fetchall(); con.close()
    if not rows:
        await edit_or_send(q,"💎 No plans found.",back_kb("admin:plans")); return
    if action=="view":
        txt="📋 Plans\n\n" + "\n\n".join(
            f"ID {p['id']} — <b>{esc(p['name'])}</b>\n₹{p['price']:g} / {p['validity_days']} days\nStatus: {'Enabled' if p['enabled'] else 'Disabled'}"
            for p in rows)
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin:plans")]])
        await edit_or_send(q,txt,kb); return
    buttons=[]
    for p in rows:
        buttons.append([InlineKeyboardButton(f"{p['id']} — {p['name']}",callback_data=f"admin:planact:{action}:{p['id']}")])
    buttons.append([InlineKeyboardButton("🔙 Back",callback_data="admin:plans")])
    await edit_or_send(q,f"Select plan for {action}.",InlineKeyboardMarkup(buttons))

async def plan_action(q, action, pid):
    if not admin_only(q): return
    con=db()
    p=con.execute("SELECT * FROM plans WHERE id=?",(pid,)).fetchone()
    if not p: con.close(); await safe_answer(q,"Plan not found.",True); return
    if action=="delete":
        con.execute("DELETE FROM plans WHERE id=?",(pid,))
        con.commit(); con.close(); await safe_answer(q,"Deleted"); await admin_plans(q); return
    if action=="toggle":
        con.execute("UPDATE plans SET enabled=? WHERE id=?",(0 if p["enabled"] else 1,pid)); con.commit(); con.close()
        await safe_answer(q,"Updated"); await admin_plans(q); return
    con.close()
    await edit_or_send(q, "✏️ Full edit flow is available through plan creation; use Delete/Disable and Add Plan to replace a plan.", back_kb("admin:plans"))

def payment_settings_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Set UPI ID",callback_data="admin:upi:set")],
        [InlineKeyboardButton("👁️ View UPI ID",callback_data="admin:upi:view"),
         InlineKeyboardButton("🗑️ Remove UPI ID",callback_data="admin:upi:remove")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ])

async def upi_set_start(q, context):
    context.user_data["awaiting_upi"]=True
    await edit_or_send(q,"💳 Send UPI ID:"); return set_flow_state(context, UPI_ID)
async def upi_set(update,context):
    if not admin_only(update): return ConversationHandler.END
    v=update.message.text.strip()
    if "@" not in v or len(v)>200:
        await update.message.reply_text(bold("❌ Invalid UPI ID."),parse_mode=ParseMode.HTML); return set_flow_state(context, UPI_ID)
    set_setting("upi_id",v); context.user_data.clear()
    await update.message.reply_text(bold("✅ UPI ID saved."),parse_mode=ParseMode.HTML,reply_markup=payment_settings_kb())
    clear_flow_state(context)
    return ConversationHandler.END

async def demo_admin(q,context):
    await edit_or_send(q,"🥵 Demo Videos",InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Demo Video",callback_data="admin:demo:add")],
        [InlineKeyboardButton("📋 View Videos",callback_data="admin:demo:view")],
        [InlineKeyboardButton("🗑️ Delete Video",callback_data="admin:demo:delete")],
        [InlineKeyboardButton("⬆️ Move Up",callback_data="admin:demo:up"),
         InlineKeyboardButton("⬇️ Move Down",callback_data="admin:demo:down")],
        [InlineKeyboardButton("👁️ Preview",callback_data="admin:demo:preview")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))

async def demo_add_start(q,context):
    context.user_data["awaiting_demo"]=True
    await edit_or_send(q,"🎥 Send a video or 📸 photo. Send /done when finished.")
    return set_flow_state(context, DEMO_MEDIA)
async def demo_add(update,context):
    if update.message.text=="/done":
        context.user_data.clear()
        await update.message.reply_text(bold("✅ Demo media saved."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:demo"))
        clear_flow_state(context)
    return ConversationHandler.END
    typ=fid=None
    if update.message.video: typ,fid="video",update.message.video.file_id
    elif update.message.photo: typ,fid="photo",update.message.photo[-1].file_id
    else:
        await update.message.reply_text(bold("❌ Send a video/photo or /done."),parse_mode=ParseMode.HTML); return set_flow_state(context, DEMO_MEDIA)
    con=db(); n=con.execute("SELECT COALESCE(MAX(sort_order),0)+1 n FROM demo_media").fetchone()["n"]
    con.execute("INSERT INTO demo_media(media_type,file_id,sort_order) VALUES(?,?,?)",(typ,fid,n));con.commit();con.close()
    await update.message.reply_text(bold("✅ Saved. Send another or /done."),parse_mode=ParseMode.HTML); return DEMO_MEDIA

async def demo_list(q):
    con=db(); rows=con.execute("SELECT * FROM demo_media ORDER BY sort_order,id").fetchall();con.close()
    txt="📋 Demo Videos\n\n"+("\n".join(f"#{i+1} ID {r['id']} — {r['media_type']}" for i,r in enumerate(rows)) if rows else "No media.")
    await edit_or_send(q,txt,back_kb("admin:demo"))

async def demo_delete_start(q,context):
    context.user_data["demo_delete"]=True
    await edit_or_send(q,"🗑️ Send Demo Media ID to delete:")
    return set_flow_state(context, DEMO_MEDIA)

async def demo_delete_msg(update,context):
    try: pid=int(update.message.text)
    except ValueError:
        await update.message.reply_text(bold("❌ Send a valid media ID."),parse_mode=ParseMode.HTML);return DEMO_MEDIA
    con=db(); con.execute("DELETE FROM demo_media WHERE id=?",(pid,)); con.commit();con.close()
    context.user_data.clear()
    await update.message.reply_text(bold("✅ Deleted if the ID existed."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:demo"))
    clear_flow_state(context)
    return ConversationHandler.END

async def tutorial_admin(q):
    await edit_or_send(q,"✅ How To Get Premium",InlineKeyboardMarkup([
        [InlineKeyboardButton("🎥 Set Tutorial Video",callback_data="admin:tutorial:video")],
        [InlineKeyboardButton("📝 Set Tutorial Text",callback_data="admin:tutorial:text")],
        [InlineKeyboardButton("✏️ Edit Tutorial",callback_data="admin:tutorial:edit")],
        [InlineKeyboardButton("🗑️ Delete Tutorial",callback_data="admin:tutorial:delete")],
        [InlineKeyboardButton("👁️ Preview",callback_data="admin:tutorial:preview")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))

async def tutorial_video_start(q,context):
    context.user_data["tutorial_video"]=True
    await edit_or_send(q,"🎥 Send tutorial video:")
    return set_flow_state(context, TUTORIAL_VIDEO)
async def tutorial_video(update,context):
    if not update.message.video:
        await update.message.reply_text(bold("❌ Send a video."),parse_mode=ParseMode.HTML);return set_flow_state(context, TUTORIAL_VIDEO)
    con=db(); con.execute("INSERT INTO tutorial(id,video_file_id,text) VALUES(1,?,COALESCE((SELECT text FROM tutorial WHERE id=1),'')) ON CONFLICT(id) DO UPDATE SET video_file_id=excluded.video_file_id",(update.message.video.file_id,))
    con.commit();con.close();context.user_data.clear()
    await update.message.reply_text(bold("✅ Tutorial video saved."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:tutorial"));return ConversationHandler.END

async def tutorial_text_start(q,context):
    context.user_data["tutorial_text"]=True; await edit_or_send(q,"📝 Send tutorial text:"); return set_flow_state(context, TUTORIAL_TEXT)
async def tutorial_text(update,context):
    con=db();con.execute("INSERT INTO tutorial(id,video_file_id,text) VALUES(1,'',?) ON CONFLICT(id) DO UPDATE SET text=excluded.text",(update.message.text,));con.commit();con.close();context.user_data.clear()
    await update.message.reply_text(bold("✅ Tutorial text saved."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:tutorial"));return ConversationHandler.END

async def tutorial_delete(q):
    con=db();con.execute("DELETE FROM tutorial WHERE id=1");con.commit();con.close()
    await edit_or_send(q,"🗑️ Tutorial deleted.",back_kb("admin:tutorial"))

async def tutorial_preview(q,context):
    await tutorial_user(q,context)

async def welcome_admin(q):
    await edit_or_send(q,"🖼️ Welcome Settings",InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Set Welcome Image",callback_data="admin:welcome:image")],
        [InlineKeyboardButton("✏️ Set Welcome Text",callback_data="admin:welcome:text")],
        [InlineKeyboardButton("👁️ Preview",callback_data="admin:welcome:preview")],
        [InlineKeyboardButton("🗑️ Remove Image",callback_data="admin:welcome:remove")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))
async def welcome_image_start(q,context):
    context.user_data["welcome_image"]=True;await edit_or_send(q,"🖼️ Send welcome image:");return set_flow_state(context, WELCOME_IMAGE)
async def welcome_image(update,context):
    if not update.message.photo:
        await update.message.reply_text(bold("❌ Send an image."),parse_mode=ParseMode.HTML);return set_flow_state(context, WELCOME_IMAGE)
    set_setting("welcome_image",update.message.photo[-1].file_id);context.user_data.clear()
    await update.message.reply_text(bold("✅ Welcome image saved."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:welcome"));return ConversationHandler.END
async def welcome_text_start(q,context):
    await edit_or_send(q,"✏️ Send welcome text:");return set_flow_state(context, WELCOME_TEXT)
async def welcome_text(update,context):
    set_setting("welcome_text",update.message.text);await update.message.reply_text(bold("✅ Welcome text saved."),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:welcome"));return ConversationHandler.END
async def welcome_preview(q,context):
    await safe_answer(q);await send_start_screen(context.bot,q.message.chat_id)

async def broadcast_admin(q):
    await edit_or_send(q,"📢 Broadcast",InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Create Broadcast",callback_data="admin:broadcast:create")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))
async def broadcast_start(q,context):
    context.user_data["broadcast"]=True
    await edit_or_send(q,"📝 Send broadcast content: text, photo, video, document, audio, voice, animation, or sticker.")
    return set_flow_state(context, BROADCAST_CONTENT)

async def capture_broadcast(update,context):
    m=update.message
    typ=fid=text=None
    if m.text: typ,text="text",m.text
    elif m.photo: typ,fid="photo",m.photo[-1].file_id; text=m.caption
    elif m.video: typ,fid="video",m.video.file_id;text=m.caption
    elif m.document: typ,fid="document",m.document.file_id;text=m.caption
    elif m.audio: typ,fid="audio",m.audio.file_id;text=m.caption
    elif m.voice: typ,fid="voice",m.voice.file_id;text=m.caption
    elif m.animation: typ,fid="animation",m.animation.file_id;text=m.caption
    elif m.sticker: typ,fid="sticker",m.sticker.file_id;text=""
    else:
        await m.reply_text(bold("❌ Unsupported content."),parse_mode=ParseMode.HTML);return set_flow_state(context, BROADCAST_CONTENT)
    context.user_data["broadcast_content"]=(typ,fid,text)
    await m.reply_text(bold("👁️ Broadcast preview:\n\n")+ (bold(esc(text)) if text else bold("Media content")) +
                       bold("\n\nChoose an action below."),parse_mode=ParseMode.HTML,
                       reply_markup=InlineKeyboardMarkup([
                           [InlineKeyboardButton("👁️ Preview",callback_data="admin:broadcast:preview"),
                            InlineKeyboardButton("✅ Send Broadcast",callback_data="admin:broadcast:send")],
                           [InlineKeyboardButton("❌ Cancel",callback_data="admin:broadcast:cancel"),
                            InlineKeyboardButton("🔙 Back",callback_data="admin:broadcast")]
                       ]))
    clear_flow_state(context)
    return ConversationHandler.END

async def broadcast_preview(q,context):
    data=context.user_data.get("broadcast_content")
    if not data: await safe_answer(q,"No draft.",True);return
    typ,fid,text=data
    try:
        if typ=="text": await context.bot.send_message(q.message.chat_id,bold(esc(text)),parse_mode=ParseMode.HTML)
        elif typ=="photo": await context.bot.send_photo(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="video": await context.bot.send_video(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="document": await context.bot.send_document(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="audio": await context.bot.send_audio(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="voice": await context.bot.send_voice(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="animation": await context.bot.send_animation(q.message.chat_id,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
        elif typ=="sticker": await context.bot.send_sticker(q.message.chat_id,fid)
    except TelegramError as e: log_event("Telegram API Errors",str(e))

async def broadcast_send(q,context):
    if not admin_only(q): return
    data=context.user_data.get("broadcast_content")
    if not data: await safe_answer(q,"No broadcast draft.",True);return
    typ,fid,text=data
    con=db(); users=con.execute("SELECT id FROM users").fetchall()
    cur=con.execute("INSERT INTO broadcasts(content_type,file_id,text,created_at,total) VALUES(?,?,?,?,?)",
                    (typ,fid,text,now_iso(),len(users))); bid=cur.lastrowid;con.commit();con.close()
    success=failed=0; log_event("Broadcast Started",f"{bid}:{len(users)}")
    for u in users:
        try:
            if typ=="text": await context.bot.send_message(u["id"],bold(esc(text)),parse_mode=ParseMode.HTML)
            elif typ=="photo": await context.bot.send_photo(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="video": await context.bot.send_video(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="document": await context.bot.send_document(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="audio": await context.bot.send_audio(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="voice": await context.bot.send_voice(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="animation": await context.bot.send_animation(u["id"],fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
            elif typ=="sticker": await context.bot.send_sticker(u["id"],fid)
            success += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after)+0.5)
            try:
                if typ=="text": await context.bot.send_message(u["id"],bold(esc(text)),parse_mode=ParseMode.HTML)
                else: await send_media(context.bot,u["id"],typ,fid,text)
                success+=1
            except TelegramError: failed+=1
        except (Forbidden,TelegramError): failed+=1
        await asyncio.sleep(0.05)
    con=db();con.execute("UPDATE broadcasts SET success=?,failed=? WHERE id=?",(success,failed,bid));con.commit();con.close()
    log_event("Broadcast Completed",f"{bid}:{success}/{failed}")
    context.user_data.pop("broadcast_content",None)
    await edit_or_send(q,f"📢 Broadcast Completed\n\nTotal: {len(users)}\nSuccess: {success}\nFailed: {failed}",back_kb("admin:menu"))

async def send_media(bot,chat,typ,fid,text):
    if typ=="photo": return await bot.send_photo(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="video": return await bot.send_video(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="document": return await bot.send_document(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="audio": return await bot.send_audio(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="voice": return await bot.send_voice(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="animation": return await bot.send_animation(chat,fid,caption=bold(esc(text or "")),parse_mode=ParseMode.HTML)
    if typ=="sticker": return await bot.send_sticker(chat,fid)

async def stats(q):
    con=db()
    vals={
        "Total Users":con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "Premium Users":con.execute("SELECT COUNT(*) c FROM users WHERE premium_until IS NOT NULL").fetchone()["c"],
        "Active Premium Users":con.execute("SELECT COUNT(*) c FROM users WHERE premium_until > ?",(now_iso(),)).fetchone()["c"],
        "Total Plans":con.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"],
        "Active Plans":con.execute("SELECT COUNT(*) c FROM plans WHERE enabled=1").fetchone()["c"],
        "Total Payments":con.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"],
        "Pending Payments":con.execute("SELECT COUNT(*) c FROM payments WHERE status='pending'").fetchone()["c"],
        "Approved Payments":con.execute("SELECT COUNT(*) c FROM payments WHERE status='approved'").fetchone()["c"],
        "Rejected Payments":con.execute("SELECT COUNT(*) c FROM payments WHERE status='rejected'").fetchone()["c"],
        "Total Revenue":con.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'").fetchone()["s"],
        "Total Demo Videos":con.execute("SELECT COUNT(*) c FROM demo_media").fetchone()["c"]
    };con.close()
    txt="📊 Statistics\n\n"+"\n".join(f"{k}: {v:g}" if isinstance(v,float) else f"{k}: {v}" for k,v in vals.items())
    await edit_or_send(q,txt,InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh",callback_data="admin:stats"),
                                                       InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]]))

async def users_menu(q):
    await edit_or_send(q,"👥 Users",InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 User List",callback_data="admin:users:list")],
        [InlineKeyboardButton("🔍 Search User",callback_data="admin:users:search")],
        [InlineKeyboardButton("👁️ View User",callback_data="admin:users:view")],
        [InlineKeyboardButton("💎 Premium Users",callback_data="admin:users:premium")],
        [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))

async def user_list(q, premium=False):
    con=db()
    rows=con.execute("SELECT * FROM users "+("WHERE premium_until > ? " if premium else "")+"ORDER BY id DESC LIMIT 50",
                     (now_iso(),) if premium else ()).fetchall();con.close()
    if not rows: await edit_or_send(q,"👥 No users found.",back_kb("admin:users"));return
    txt="👥 Users\n\n"+"\n\n".join(
        f"🆔 {r['id']}\nUsername: @{esc(r['username'] or '')}\nName: {esc((r['first_name'] or '')+' '+(r['last_name'] or ''))}\n"
        f"Registered: {esc(r['registered_at'])}\nPremium Until: {esc(r['premium_until'] or 'None')}\nStatus: {esc(r['status'])}"
        for r in rows)
    await edit_or_send(q,txt,back_kb("admin:users"))

async def logs_menu(q):
    con=db();rows=con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 30").fetchall();con.close()
    txt="📋 Recent Logs\n\n"+"\n".join(f"{r['created_at']} — {esc(r['event'])} — {esc(r['details'])}" for r in rows) if rows else "📋 No logs."
    await edit_or_send(q,txt,InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh",callback_data="admin:logs"),
                                                      InlineKeyboardButton("🗑️ Clear Logs",callback_data="admin:logs:clear")],
                                                     [InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]]))

async def settings_menu(q):
    await edit_or_send(q,"⚙️ Bot Settings",InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Test Bot",callback_data="admin:test")],
        [InlineKeyboardButton("🔄 Refresh",callback_data="admin:settings"),
         InlineKeyboardButton("🔙 Back",callback_data="admin:menu")]
    ]))

async def payment_requests(q):
    con=db();rows=con.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT 50").fetchall();con.close()
    if not rows: await edit_or_send(q,"💰 No pending payment requests.",back_kb("admin:menu"));return
    buttons=[[InlineKeyboardButton(f"🧾 Payment #{r['id']} — ₹{r['amount']:g}",
                                    callback_data=f"admin:payment:view:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 Back",callback_data="admin:menu")])
    await edit_or_send(q,"💰 Pending Payment Requests",InlineKeyboardMarkup(buttons))

async def payment_view(q,context,pid):
    con=db();r=con.execute("SELECT * FROM payments WHERE id=?",(pid,)).fetchone();con.close()
    if not r: await safe_answer(q,"Payment not found.",True);return
    txt=f"🧾 Payment #{pid}\n\n👤 User ID: {r['user_id']}\n💎 Plan: {esc(r['plan_name'])}\n💰 Amount: ₹{r['amount']:g}\n⏳ Validity: {r['validity']} Days\nStatus: {esc(r['status'])}"
    try:
        if r["screenshot_file_id"]:
            await context.bot.send_photo(q.message.chat_id,r["screenshot_file_id"],caption=bold(txt),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",callback_data=f"admin:payment:approve:{pid}"),
                                                     InlineKeyboardButton("❌ Reject",callback_data=f"admin:payment:reject:{pid}")],
                                                    [InlineKeyboardButton("🔙 Back",callback_data="admin:payments")]]))
        else: await edit_or_send(q,txt,back_kb("admin:payments"))
    except TelegramError: await edit_or_send(q,txt,back_kb("admin:payments"))

async def callback(update,context):
    q=update.callback_query
    data=q.data or ""
    if data.startswith("admin:") and not admin_only(update):
        await safe_answer(q,"❌ Access Denied",True);return
    try:
        if data=="user:premium":
            await safe_answer(q)
            await context.bot.send_message(
                q.message.chat_id,
                bold("══════« CHOSE PLAN ✅»═════"),
                parse_mode=ParseMode.HTML,
                reply_markup=main_user_kb(),
            )
        elif data=="user:back": await safe_answer(q); await send_start_screen(context.bot,q.message.chat_id)
        elif data=="user:demo": await demo_user(q,context)
        elif data=="user:tutorial": await tutorial_user(q,context)
        elif data.startswith("user:plan:"): await send_plan(q,context,int(data.split(":")[-1]))
        elif data.startswith("user:payment:paid:"): await request_payment(update,context,int(data.split(":")[-1]))
        elif data=="admin:menu": await safe_answer(q); await edit_or_send(q,"🔐 Admin Panel",admin_menu_kb())
        elif data=="admin:stats": await stats(q)
        elif data=="admin:plans": await admin_plans(q)
        elif data=="admin:getpremium": await admin_getpremium(q)
        elif data.startswith("admin:getpremium:plan:"):
            await send_plan(q, context, int(data.split(":")[-1]))
        elif data=="admin:plans:add": await plan_add_start(q,context)
        elif data=="admin:plans:view": await select_plan_by_action(q,"view")
        elif data in ("admin:plans:delete","admin:plans:toggle","admin:plans:edit"): await select_plan_by_action(q,data.split(":")[-1])
        elif data.startswith("admin:planact:"): await plan_action(q,data.split(":")[2],int(data.split(":")[3]))
        elif data=="admin:paysettings": await edit_or_send(q,"💳 Payment Settings",payment_settings_kb())
        elif data=="admin:upi:set": return await upi_set_start(q,context)
        elif data=="admin:upi:view": await edit_or_send(q,f"💳 UPI ID: {esc(get_setting('upi_id','Not configured'))}",payment_settings_kb())
        elif data=="admin:upi:remove": set_setting("upi_id",""); await edit_or_send(q,"🗑️ UPI ID removed.",payment_settings_kb())
        elif data=="admin:payments": await payment_requests(q)
        elif data.startswith("admin:payment:view:"): await payment_view(q,context,int(data.split(":")[-1]))
        elif data.startswith("admin:payment:approve:"): await approve_payment(q,context,int(data.split(":")[-1]))
        elif data.startswith("admin:payment:reject:"): return await reject_payment(q,context,int(data.split(":")[-1]))
        elif data=="admin:demo": await demo_admin(q,context)
        elif data=="admin:demo:add": return await demo_add_start(q,context)
        elif data=="admin:demo:view": await demo_list(q)
        elif data=="admin:demo:delete": return await demo_delete_start(q,context)
        elif data in ("admin:demo:up","admin:demo:down"):
            await edit_or_send(q,"⬆️⬇️ Move controls require a media ID; use View Videos to inspect IDs.",back_kb("admin:demo"))
        elif data=="admin:demo:preview": await demo_user(q,context)
        elif data=="admin:tutorial": await tutorial_admin(q)
        elif data=="admin:tutorial:video": return await tutorial_video_start(q,context)
        elif data=="admin:tutorial:text": return await tutorial_text_start(q,context)
        elif data=="admin:tutorial:edit": await tutorial_text_start(q,context)
        elif data=="admin:tutorial:delete": await tutorial_delete(q)
        elif data=="admin:tutorial:preview": await tutorial_preview(q,context)
        elif data=="admin:welcome": await welcome_admin(q)
        elif data=="admin:welcome:image": return await welcome_image_start(q,context)
        elif data=="admin:welcome:text": return await welcome_text_start(q,context)
        elif data=="admin:welcome:preview": await welcome_preview(q,context)
        elif data=="admin:welcome:remove": set_setting("welcome_image",""); await edit_or_send(q,"🗑️ Welcome image removed.",back_kb("admin:welcome"))
        elif data=="admin:broadcast": await broadcast_admin(q)
        elif data=="admin:broadcast:create": return await broadcast_start(q,context)
        elif data=="admin:broadcast:preview": await broadcast_preview(q,context)
        elif data=="admin:broadcast:send": await broadcast_send(q,context)
        elif data=="admin:broadcast:cancel": context.user_data.pop("broadcast_content",None); await edit_or_send(q,"❌ Broadcast cancelled.",back_kb("admin:broadcast"))
        elif data=="admin:users": await users_menu(q)
        elif data=="admin:users:list": await user_list(q)
        elif data=="admin:users:premium": await user_list(q,True)
        elif data in ("admin:users:search","admin:users:view"):
            await edit_or_send(q,"🔍 User search/view is available by ID. Send the numeric Telegram User ID.",back_kb("admin:users"))
            context.user_data["awaiting_user_id"]=True
        elif data=="admin:logs": await logs_menu(q)
        elif data=="admin:logs:clear": 
            con=db();con.execute("DELETE FROM logs");con.commit();con.close();await logs_menu(q)
        elif data=="admin:settings": await settings_menu(q)
        elif data=="admin:test":
            try:
                me=await context.bot.get_me(); await edit_or_send(q,f"🧪 Test Bot\n\nTelegram API: OK\nBot: @{esc(me.username)}",back_kb("admin:settings"))
            except TelegramError as e: log_event("Telegram API Errors",str(e)); await edit_or_send(q,"❌ Telegram API test failed.",back_kb("admin:settings"))
        else:
            await safe_answer(q,"Invalid or expired action.",True)
    except Exception as e:
        logger.exception("callback error")
        log_event("Telegram API Errors",str(e))
        try: await safe_answer(q,"❌ Something went wrong.",True)
        except Exception: pass

async def admin_user_id_message(update,context):
    if not admin_only(update) or not context.user_data.get("awaiting_user_id"): return False
    try: uid=int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(bold("❌ Invalid User ID."),parse_mode=ParseMode.HTML);return True
    con=db();r=con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();con.close()
    context.user_data.pop("awaiting_user_id",None)
    if not r: await update.message.reply_text(bold("❌ User not found."),parse_mode=ParseMode.HTML);return True
    await update.message.reply_text(bold(f"👁️ User\n\n🆔 {r['id']}\nUsername: @{esc(r['username'] or '')}\n"
        f"First Name: {esc(r['first_name'] or '')}\nLast Name: {esc(r['last_name'] or '')}\nRegistration Date: {esc(r['registered_at'])}\n"
        f"Premium Until: {esc(r['premium_until'] or 'None')}\nStatus: {esc(r['status'])}"),parse_mode=ParseMode.HTML,reply_markup=back_kb("admin:users"))
    return True

async def handle_flow_message(update, context):
    state = context.user_data.get("_flow_state")
    if not state:
        return False

    handlers = {
        PLAN_NAME: plan_name,
        PLAN_PRICE: plan_price,
        PLAN_DAYS: plan_days,
        PLAN_DESC: plan_desc,
        PLAN_LINK: plan_link,
        PLAN_IMAGE: plan_image,
        UPI_ID: upi_set,
        TUTORIAL_VIDEO: tutorial_video,
        TUTORIAL_TEXT: tutorial_text,
        WELCOME_IMAGE: welcome_image,
        WELCOME_TEXT: welcome_text,
        BROADCAST_CONTENT: capture_broadcast,
    }

    if state == DEMO_MEDIA:
        if context.user_data.get("demo_delete"):
            result = await demo_delete_msg(update, context)
        elif context.user_data.get("awaiting_demo"):
            result = await demo_add(update, context)
        else:
            return False
    else:
        handler = handlers.get(state)
        if not handler:
            return False
        result = await handler(update, context)

    if result == ConversationHandler.END:
        clear_flow_state(context)
    elif isinstance(result, int):
        set_flow_state(context, result)
    return True


async def message_router(update,context):
    if not update.message: return
    if await handle_flow_message(update, context): return
    if update.message.text == "💎 GET PREMIUM":
        user_registered(update.effective_user)
        await context.bot.send_message(
            update.effective_chat.id,
            bold("══════« CHOSE PLAN ✅»═════"),
            parse_mode=ParseMode.HTML,
            reply_markup=main_user_kb(),
        )
        return
    if update.message.text == "🥵 DEMO":
        user_registered(update.effective_user)
        con = db()
        rows = con.execute("SELECT * FROM demo_media ORDER BY sort_order,id").fetchall()
        con.close()
        if not rows:
            await update.message.reply_text(bold("🥵 No demo videos are configured yet."), parse_mode=ParseMode.HTML)
            return
        for r in rows:
            try:
                if r["media_type"] == "video":
                    await context.bot.send_video(update.effective_chat.id, r["file_id"])
                else:
                    await context.bot.send_photo(update.effective_chat.id, r["file_id"])
            except TelegramError:
                continue
        return
    if await process_rejection(update,context): return
    if await admin_user_id_message(update,context): return
    if await handle_payment_photo(update,context): return

async def error_handler(update,context):
    err=context.error
    logger.exception("Unhandled error: %s",err)
    log_event("Telegram API Errors",str(err)[:1000])

async def post_init(app):
    init_db()
    log_event("Bot Started")
    try:
        me=await app.bot.get_me()
        logger.info("Bot started as @%s",me.username)
    except TelegramError as e:
        log_event("Telegram API Errors",str(e))

def build_app():
    if not BOT_TOKEN or BOT_TOKEN=="PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN in bot.py before running.")
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_cmd))
    app.add_handler(CallbackQueryHandler(callback))

    # Admin/user input flows are handled with explicit states.
    app.add_handler(ConversationHandler(
        entry_points=[],
        states={
            PLAN_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,plan_name)],
            PLAN_PRICE:[MessageHandler(filters.TEXT & ~filters.COMMAND,plan_price)],
            PLAN_DAYS:[MessageHandler(filters.TEXT & ~filters.COMMAND,plan_days)],
            PLAN_DESC:[MessageHandler(filters.TEXT & ~filters.COMMAND,plan_desc)],
            PLAN_LINK:[MessageHandler(filters.TEXT,plan_link)],
            PLAN_IMAGE:[MessageHandler(filters.PHOTO | (filters.TEXT & filters.COMMAND),plan_image)],
            UPI_ID:[MessageHandler(filters.TEXT & ~filters.COMMAND,upi_set)],
            DEMO_MEDIA:[MessageHandler(filters.PHOTO | filters.VIDEO | (filters.TEXT & filters.COMMAND),demo_add)],
            TUTORIAL_VIDEO:[MessageHandler(filters.VIDEO, tutorial_video)],
            TUTORIAL_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND,tutorial_text)],
            WELCOME_IMAGE:[MessageHandler(filters.PHOTO,welcome_image)],
            WELCOME_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND,welcome_text)],
            BROADCAST_CONTENT:[MessageHandler(filters.ALL & ~filters.COMMAND,capture_broadcast)],
        },
        fallbacks=[CommandHandler("cancel",cancel_flow())],
        allow_reentry=True
    ))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router), group=10)
    app.add_error_handler(error_handler)
    return app

if __name__=="__main__":
    init_db()
    application=build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
