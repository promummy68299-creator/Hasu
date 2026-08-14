import os
import sys
import logging
import sqlite3
import io
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import telebot
from telebot import types

import qrcode
from PIL import Image

# ---------- CONFIG ----------
BOT_TOKEN = "8710908239:AAGQt-X2jVB3HzXVaTs63PZKRkhr3MD4peY"   # Replace
ADMIN_ID = 7709767483                # Replace

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("❌ Set BOT_TOKEN in bot.py")
    sys.exit(1)

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.info("🚀 PREMIUM BOT STARTING (telebot)")

# ---------- DATABASE ----------
DB_PATH = "bot_data.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            premium_until TIMESTAMP,
            is_premium BOOLEAN DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price INTEGER NOT NULL,
            validity INTEGER NOT NULL,
            link TEXT,
            description TEXT,
            enabled BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS demo_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            plan_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            validity INTEGER NOT NULL,
            screenshot_file_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMP,
            rejected_at TIMESTAMP
        )
    ''')
    defaults = [
        ('welcome_image', ''),
        ('welcome_text', 'Welcome to Premium Bot!'),
        ('plans_image', ''),          # New: image shown before plans list
        ('upi_id', ''),
        ('tutorial_video', ''),
        ('tutorial_text', 'How to get premium:'),
    ]
    for key, val in defaults:
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

init_db()
logger.info("✅ Tables verified")

# ---------- DB HELPERS ----------
def get_setting(key):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else None

def set_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def add_user(user_id, username='', first_name=''):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
              (user_id, username, first_name))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_all_plans(enabled_only=True):
    conn = get_db()
    c = conn.cursor()
    query = 'SELECT * FROM plans'
    if enabled_only:
        query += ' WHERE enabled = 1'
    query += ' ORDER BY price ASC'
    c.execute(query)
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_plan(plan_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM plans WHERE id = ?', (plan_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def add_plan(name, price, validity, link, description=''):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO plans (name, price, validity, link, description) VALUES (?, ?, ?, ?, ?)',
              (name, price, validity, link, description))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid

def update_plan(plan_id, **kwargs):
    conn = get_db()
    c = conn.cursor()
    fields = []
    vals = []
    for k, v in kwargs.items():
        if v is not None:
            fields.append(f"{k} = ?")
            vals.append(v)
    if fields:
        vals.append(plan_id)
        c.execute(f"UPDATE plans SET {', '.join(fields)} WHERE id = ?", vals)
        conn.commit()
    conn.close()

def delete_plan(plan_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM plans WHERE id = ?', (plan_id,))
    conn.commit()
    conn.close()

def toggle_plan(plan_id, enabled):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE plans SET enabled = ? WHERE id = ?', (enabled, plan_id))
    conn.commit()
    conn.close()

def get_demo_media():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM demo_media ORDER BY position ASC')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def add_demo_media(media_type, file_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT MAX(position) as max_pos FROM demo_media')
    row = c.fetchone()
    pos = (row['max_pos'] or 0) + 1
    c.execute('INSERT INTO demo_media (media_type, file_id, position) VALUES (?, ?, ?)',
              (media_type, file_id, pos))
    conn.commit()
    conn.close()

def delete_demo_media(media_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM demo_media WHERE id = ?', (media_id,))
    conn.commit()
    conn.close()

def clear_demo_media():
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM demo_media')
    conn.commit()
    conn.close()

def add_payment(user_id, plan_id, plan_name, amount, validity, file_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO payments (user_id, plan_id, plan_name, amount, validity, screenshot_file_id)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, plan_id, plan_name, amount, validity, file_id))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    return pid

def get_pending_payments():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT p.*, u.username, u.first_name
        FROM payments p
        JOIN users u ON p.user_id = u.user_id
        WHERE p.status = 'pending'
        ORDER BY p.created_at ASC
    ''')
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_payment(payment_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_payment_status(payment_id, status):
    conn = get_db()
    c = conn.cursor()
    if status == 'approved':
        c.execute('UPDATE payments SET status = ?, approved_at = CURRENT_TIMESTAMP WHERE id = ?',
                  (status, payment_id))
    elif status == 'rejected':
        c.execute('UPDATE payments SET status = ?, rejected_at = CURRENT_TIMESTAMP WHERE id = ?',
                  (status, payment_id))
    else:
        c.execute('UPDATE payments SET status = ? WHERE id = ?', (status, payment_id))
    conn.commit()
    conn.close()

def update_user_premium(user_id, until):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET premium_until = ?, is_premium = 1 WHERE user_id = ?',
              (until, user_id))
    conn.commit()
    conn.close()

def get_statistics():
    conn = get_db()
    c = conn.cursor()
    stats = {}
    c.execute('SELECT COUNT(*) FROM users')
    stats['total_users'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM users WHERE is_premium = 1')
    stats['premium_users'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM users WHERE is_premium = 1 AND premium_until > datetime("now")')
    stats['active_premium'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM plans WHERE enabled = 1')
    stats['active_plans'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM plans')
    stats['total_plans'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM demo_media')
    stats['demo_media'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "pending"')
    stats['pending_payments'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "approved"')
    stats['approved_payments'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM payments WHERE status = "rejected"')
    stats['rejected_payments'] = c.fetchone()[0]
    conn.close()
    return stats

# ---------- QR GENERATOR (SIMPLE & RELIABLE) ----------
def normalize_price(price):
    if price is None:
        raise ValueError("Price is None")
    if isinstance(price, str):
        price = price.replace('₹', '').replace(',', '').strip()
        if not price:
            raise ValueError("Empty price")
        try:
            price = float(price)
        except ValueError:
            raise ValueError(f"Invalid price: {price}")
    if isinstance(price, (int, float)):
        if price <= 0:
            raise ValueError(f"Non-positive price: {price}")
        if price == int(price):
            return str(int(price))
        return f"{price:.2f}"
    raise ValueError(f"Unsupported price type: {type(price)}")

def generate_upi_qr(upi_id, price, plan_name="Premium"):
    if not upi_id or not upi_id.strip():
        raise ValueError("UPI ID is empty")
    price_str = normalize_price(price)
    params = {
        "pa": upi_id.strip(),
        "pn": plan_name[:50],
        "am": price_str,
        "cu": "INR"
    }
    upi_uri = "upi://pay?" + urlencode(params)
    logger.info(f"QR URI: {upi_uri[:60]}...")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    logger.info("QR generated successfully")
    return buf

logger.info("✅ QR SYSTEM READY")

# ---------- BOT ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
user_data = {}

# ---------- KEYBOARDS ----------
def main_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(types.KeyboardButton("✨ Get Premium ✨"))
    return kb

def user_plans_keyboard(plans):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in plans:
        kb.add(types.InlineKeyboardButton(f"💎 {p['name']} — ₹{p['price']}", callback_data=f"select_plan_{p['id']}"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    return kb

def plan_payment_keyboard(plan_id, plan_link=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ I Have Paid", callback_data=f"pay_plan_{plan_id}"))
    if plan_link and plan_link.startswith(('http://', 'https://')):
        kb.add(types.InlineKeyboardButton("🔗 Open Plan Link", url=plan_link))
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel"))
    return kb

def admin_main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🖼 Welcome Settings", callback_data="admin_welcome"))
    kb.add(types.InlineKeyboardButton("💎 Manage Premium Plans", callback_data="admin_plans"))
    kb.add(types.InlineKeyboardButton("🔥 Manage Premium Demo", callback_data="admin_demo"))
    kb.add(types.InlineKeyboardButton("💦 How To Get Premium", callback_data="admin_tutorial"))
    kb.add(types.InlineKeyboardButton("💳 Payment Settings", callback_data="admin_payment_settings"))
    kb.add(types.InlineKeyboardButton("💰 Payment Requests", callback_data="admin_payments"))
    kb.add(types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"))
    kb.add(types.InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"))
    kb.add(types.InlineKeyboardButton("❌ Close", callback_data="admin_close"))
    return kb

def admin_plans_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Add Plan", callback_data="admin_add_plan"))
    kb.add(types.InlineKeyboardButton("✏️ Edit Plan", callback_data="admin_edit_plan"))
    kb.add(types.InlineKeyboardButton("🗑 Delete Plan", callback_data="admin_delete_plan"))
    kb.add(types.InlineKeyboardButton("📋 View Plans", callback_data="admin_view_plans"))
    kb.add(types.InlineKeyboardButton("🔄 Enable/Disable", callback_data="admin_toggle_plan"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
    return kb

def plan_list_keyboard(plans, action):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in plans:
        kb.add(types.InlineKeyboardButton(f"{p['name']} — ₹{p['price']}", callback_data=f"{action}_{p['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_plans"))
    return kb

def plan_edit_keyboard(plan_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✏️ Name", callback_data=f"edit_name_{plan_id}"))
    kb.add(types.InlineKeyboardButton("💰 Price", callback_data=f"edit_price_{plan_id}"))
    kb.add(types.InlineKeyboardButton("⏳ Validity", callback_data=f"edit_validity_{plan_id}"))
    kb.add(types.InlineKeyboardButton("🔗 Link", callback_data=f"edit_link_{plan_id}"))
    kb.add(types.InlineKeyboardButton("📝 Description", callback_data=f"edit_desc_{plan_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_plans"))
    return kb

def admin_welcome_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🖼 Set Welcome Image", callback_data="admin_set_welcome_image"))
    kb.add(types.InlineKeyboardButton("🖼 Set Plans Image", callback_data="admin_set_plans_image"))
    kb.add(types.InlineKeyboardButton("✏️ Set Welcome Text", callback_data="admin_set_welcome_text"))
    kb.add(types.InlineKeyboardButton("👁 Preview Welcome", callback_data="admin_preview_welcome"))
    kb.add(types.InlineKeyboardButton("🗑 Remove Welcome Image", callback_data="admin_remove_welcome_image"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
    return kb

def admin_demo_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Add Media", callback_data="admin_add_demo"))
    kb.add(types.InlineKeyboardButton("📋 View Media", callback_data="admin_view_demo"))
    kb.add(types.InlineKeyboardButton("🗑 Delete Media", callback_data="admin_delete_demo"))
    kb.add(types.InlineKeyboardButton("🗑 Delete All", callback_data="admin_clear_demo"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
    return kb

def admin_tutorial_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🎥 Set Video", callback_data="admin_set_tutorial_video"))
    kb.add(types.InlineKeyboardButton("✏️ Set Text", callback_data="admin_set_tutorial_text"))
    kb.add(types.InlineKeyboardButton("👁 Preview", callback_data="admin_preview_tutorial"))
    kb.add(types.InlineKeyboardButton("🗑 Delete", callback_data="admin_delete_tutorial"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
    return kb

def admin_payment_settings_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔗 Set UPI", callback_data="admin_set_upi"))
    kb.add(types.InlineKeyboardButton("👁 View UPI", callback_data="admin_view_upi"))
    kb.add(types.InlineKeyboardButton("🗑 Remove UPI", callback_data="admin_remove_upi"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_back"))
    return kb

def confirm_delete_keyboard(plan_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_del_{plan_id}"))
    kb.add(types.InlineKeyboardButton("❌ Cancel", callback_data="admin_plans"))
    return kb

# ---------- COMMANDS ----------
@bot.message_handler(commands=['start'])
def start_cmd(message):
    user = message.from_user
    add_user(user.id, user.username or '', user.first_name or '')
    logger.info(f"/start from {user.id}")
    welcome_image = get_setting('welcome_image')
    welcome_text = get_setting('welcome_text') or 'Welcome to Premium Bot!'
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💎 Get Premium", callback_data="get_premium"))
    kb.add(types.InlineKeyboardButton("🔥 Premium Demo", callback_data="demo_premium"))
    kb.add(types.InlineKeyboardButton("💦 How To Get Premium", callback_data="how_to_premium"))
    if welcome_image:
        bot.send_photo(user.id, welcome_image, caption=welcome_text, reply_markup=kb)
    else:
        bot.send_message(user.id, welcome_text, reply_markup=kb)
    bot.send_message(user.id, "Use buttons or press ✨ Get Premium ✨", reply_markup=main_menu_keyboard())

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        bot.reply_to(message, "❌ Unauthorized")
        return
    logger.info(f"Admin panel opened by {user_id}")
    bot.send_message(user_id, "⚙️ *ADMIN PANEL*", reply_markup=admin_main_keyboard())

@bot.message_handler(func=lambda m: m.text == "✨ Get Premium ✨")
def get_premium_button(message):
    plans = get_all_plans(enabled_only=True)
    if not plans:
        bot.send_message(message.chat.id, "No plans available.")
        return
    # Send plans image if set
    plans_image = get_setting('plans_image')
    if plans_image:
        bot.send_photo(message.chat.id, plans_image, caption="💎 *Choose your plan*", parse_mode='HTML')
    bot.send_message(message.chat.id, "💎 *Premium Plans*", reply_markup=user_plans_keyboard(plans), parse_mode='HTML')

# ---------- CALLBACK HANDLER ----------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"Callback: {data} from {user_id}")

    if data == "get_premium":
        plans = get_all_plans(enabled_only=True)
        if not plans:
            bot.send_message(user_id, "No plans available.")
            bot.answer_callback_query(call.id)
            return
        # Send plans image if set
        plans_image = get_setting('plans_image')
        if plans_image:
            # Delete the original message to avoid clutter, or send a new one
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_photo(user_id, plans_image, caption="💎 *Choose your plan*", parse_mode='HTML')
            bot.send_message(user_id, "💎 *Premium Plans*", reply_markup=user_plans_keyboard(plans), parse_mode='HTML')
        else:
            bot.edit_message_text("💎 *Premium Plans*", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=user_plans_keyboard(plans), parse_mode='HTML')
        bot.answer_callback_query(call.id)
        return

    if data.startswith("select_plan_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found")
            return
        upi = get_setting('upi_id')
        if not upi:
            bot.edit_message_text("⚠️ Payment unavailable – contact admin.",
                                  chat_id=call.message.chat.id, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return
        try:
            logger.info(f"QR generation | plan={plan_id}, price={plan['price']}, upi_set={bool(upi)}")
            qr_buf = generate_upi_qr(upi, plan['price'], plan['name'])
            caption = (f"📦 Plan: {plan['name']}\n💰 ₹{plan['price']}\n⏳ {plan['validity']} days\n"
                       f"💳 UPI: {upi}\n\nScan QR to pay")
            # Validate link
            link = plan.get('link')
            if link and not (link.startswith('http://') or link.startswith('https://')):
                link = None
            bot.send_photo(user_id, qr_buf, caption=caption,
                           reply_markup=plan_payment_keyboard(plan_id, link))
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "QR generated ✅")
            logger.info("QR sent successfully")
        except Exception as e:
            logger.exception("QR generation failed")
            bot.send_message(user_id, "❌ QR generation failed. Contact admin.")
            bot.answer_callback_query(call.id, "Error")
        return

    if data == "pay_cancel":
        bot.edit_message_text("Cancelled.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("pay_plan_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found")
            return
        user_data[user_id] = {'payment_plan': plan_id}
        bot.edit_message_text("📸 Send your payment screenshot (photo).", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "demo_premium":
        media_list = get_demo_media()
        if not media_list:
            bot.send_message(user_id, "No demo media available.")
            bot.answer_callback_query(call.id)
            return
        for m in media_list:
            try:
                if m['media_type'] == 'photo':
                    bot.send_photo(user_id, m['file_id'])
                else:
                    bot.send_video(user_id, m['file_id'])
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Demo send error: {e}")
        bot.send_message(user_id, "That's all!", reply_markup=main_menu_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "how_to_premium":
        video = get_setting('tutorial_video')
        txt = get_setting('tutorial_text') or "How to get premium:"
        if video:
            bot.send_video(user_id, video, caption=txt)
        else:
            bot.send_message(user_id, txt + "\n\n(No tutorial video set)")
        bot.answer_callback_query(call.id)
        return

    if data == "back_main":
        bot.edit_message_text("Back to main menu.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=main_menu_keyboard())
        bot.answer_callback_query(call.id)
        return

    # ---------- ADMIN ----------
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return

    if data == "admin_welcome":
        bot.edit_message_text("🖼 Welcome Settings", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_welcome_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_plans":
        bot.edit_message_text("💎 Manage Plans", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_plans_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_demo":
        bot.edit_message_text("🔥 Manage Demo", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_demo_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_tutorial":
        bot.edit_message_text("💦 Tutorial", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_tutorial_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_payment_settings":
        bot.edit_message_text("💳 Payment Settings", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_payment_settings_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_payments":
        pending = get_pending_payments()
        if not pending:
            bot.edit_message_text("No pending payments.", chat_id=call.message.chat.id, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for p in pending:
            label = f"#{p['id']} - {p.get('username') or p.get('first_name')} - ₹{p['amount']}"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"view_payment_{p['id']}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
        bot.edit_message_text("💰 Pending Payments", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_broadcast":
        user_data[user_id] = {'broadcast': True}
        bot.edit_message_text("📢 Send the message/media to broadcast.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_stats":
        stats = get_statistics()
        text = (f"📊 *Statistics*\n👥 Users: {stats['total_users']}\n"
                f"👑 Premium: {stats['premium_users']}\n✅ Active: {stats['active_premium']}\n"
                f"📦 Plans: {stats['total_plans']} (active {stats['active_plans']})\n"
                f"🖼 Demo: {stats['demo_media']}\n"
                f"💰 Pending: {stats['pending_payments']}, Approved: {stats['approved_payments']}, Rejected: {stats['rejected_payments']}")
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("🔙 Back", callback_data="admin_back")),
                              parse_mode='HTML')
        bot.answer_callback_query(call.id)
        return

    if data == "admin_close":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_back":
        bot.edit_message_text("⚙️ ADMIN PANEL", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_main_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Welcome Settings – Set Welcome Image
    if data == "admin_set_welcome_image":
        user_data[user_id] = {'set': 'welcome_image'}
        bot.edit_message_text("Send new welcome image (photo).", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    # Welcome Settings – Set Plans Image
    if data == "admin_set_plans_image":
        user_data[user_id] = {'set': 'plans_image'}
        bot.edit_message_text("Send the image to show before the plans list (photo).", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_set_welcome_text":
        user_data[user_id] = {'set': 'welcome_text'}
        bot.edit_message_text("Send new welcome text.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_preview_welcome":
        img = get_setting('welcome_image')
        txt = get_setting('welcome_text') or 'Welcome!'
        if img:
            bot.send_photo(user_id, img, caption=txt)
        else:
            bot.send_message(user_id, txt)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_remove_welcome_image":
        set_setting('welcome_image', '')
        bot.answer_callback_query(call.id, "Welcome image removed")
        return

    # Plans Management
    if data == "admin_add_plan":
        user_data[user_id] = {'add_plan': True, 'step': 'name'}
        bot.edit_message_text("📝 Enter plan name:", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_view_plans":
        plans = get_all_plans(enabled_only=False)
        if not plans:
            text = "No plans."
        else:
            text = "📋 All Plans:\n\n"
            for p in plans:
                status = "✅" if p['enabled'] else "❌"
                text += f"{status} {p['name']} - ₹{p['price']} - {p['validity']}d\n"
                if p.get('link'):
                    text += f"🔗 {p['link']}\n"
                text += "\n"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("⬅️ Back", callback_data="admin_plans")))
        bot.answer_callback_query(call.id)
        return

    if data == "admin_edit_plan":
        plans = get_all_plans(enabled_only=False)
        if not plans:
            bot.edit_message_text("No plans.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_plans_keyboard())
            bot.answer_callback_query(call.id)
            return
        bot.edit_message_text("Select plan to edit:", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=plan_list_keyboard(plans, "edit_plan"))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_plan_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found")
            return
        user_data[user_id] = {'edit_plan': plan_id}
        text = f"Editing {plan['name']}\nChoose field:"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=plan_edit_keyboard(plan_id))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_name_"):
        plan_id = int(data.split("_")[2])
        user_data[user_id] = {'edit_plan': plan_id, 'field': 'name'}
        bot.edit_message_text("Enter new name:", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_price_"):
        plan_id = int(data.split("_")[2])
        user_data[user_id] = {'edit_plan': plan_id, 'field': 'price'}
        bot.edit_message_text("Enter new price:", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_validity_"):
        plan_id = int(data.split("_")[2])
        user_data[user_id] = {'edit_plan': plan_id, 'field': 'validity'}
        bot.edit_message_text("Enter new validity (days):", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_link_"):
        plan_id = int(data.split("_")[2])
        user_data[user_id] = {'edit_plan': plan_id, 'field': 'link'}
        bot.edit_message_text("Enter new link (or 'skip'):", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("edit_desc_"):
        plan_id = int(data.split("_")[2])
        user_data[user_id] = {'edit_plan': plan_id, 'field': 'desc'}
        bot.edit_message_text("Enter new description:", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_delete_plan":
        plans = get_all_plans(enabled_only=False)
        if not plans:
            bot.edit_message_text("No plans.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_plans_keyboard())
            bot.answer_callback_query(call.id)
            return
        bot.edit_message_text("Select plan to delete:", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=plan_list_keyboard(plans, "del_plan"))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("del_plan_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found")
            return
        bot.edit_message_text(f"⚠️ Delete {plan['name']}?", chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=confirm_delete_keyboard(plan_id))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("confirm_del_"):
        plan_id = int(data.split("_")[2])
        delete_plan(plan_id)
        bot.edit_message_text("Plan deleted.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_plans_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_toggle_plan":
        plans = get_all_plans(enabled_only=False)
        if not plans:
            bot.edit_message_text("No plans.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_plans_keyboard())
            bot.answer_callback_query(call.id)
            return
        bot.edit_message_text("Toggle plan:", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=plan_list_keyboard(plans, "toggle_plan"))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("toggle_plan_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found")
            return
        new_enabled = not plan['enabled']
        toggle_plan(plan_id, new_enabled)
        bot.edit_message_text(f"Plan {plan['name']} {'enabled' if new_enabled else 'disabled'}.",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_plans_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Demo
    if data == "admin_add_demo":
        user_data[user_id] = {'add_demo': True}
        bot.edit_message_text("Send photo or video. Press Done when finished.",
                              chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("✅ Done", callback_data="demo_done"),
                                  types.InlineKeyboardButton("❌ Cancel", callback_data="demo_cancel")
                              ))
        bot.answer_callback_query(call.id)
        return

    if data == "demo_done":
        bot.edit_message_text("✅ Media added.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_demo_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "demo_cancel":
        if user_id in user_data and 'add_demo' in user_data[user_id]:
            del user_data[user_id]['add_demo']
        bot.edit_message_text("Cancelled.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_demo_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_view_demo":
        media = get_demo_media()
        if not media:
            text = "No demo media."
        else:
            text = "📋 Demo Media:\n"
            for m in media:
                text += f"ID {m['id']} - {m['media_type']} (pos {m['position']})\n"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("⬅️ Back", callback_data="admin_demo")))
        bot.answer_callback_query(call.id)
        return

    if data == "admin_delete_demo":
        media = get_demo_media()
        if not media:
            bot.edit_message_text("No media.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  reply_markup=admin_demo_keyboard())
            bot.answer_callback_query(call.id)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for m in media:
            kb.add(types.InlineKeyboardButton(f"Delete #{m['id']}", callback_data=f"del_demo_{m['id']}"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_demo"))
        bot.edit_message_text("Select to delete:", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("del_demo_"):
        media_id = int(data.split("_")[2])
        delete_demo_media(media_id)
        bot.edit_message_text("Deleted.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_demo_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_clear_demo":
        clear_demo_media()
        bot.edit_message_text("All demo media cleared.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=admin_demo_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Tutorial
    if data == "admin_set_tutorial_video":
        user_data[user_id] = {'set': 'tutorial_video'}
        bot.edit_message_text("Send tutorial video (with optional caption).", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_set_tutorial_text":
        user_data[user_id] = {'set': 'tutorial_text'}
        bot.edit_message_text("Send tutorial text.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_preview_tutorial":
        video = get_setting('tutorial_video')
        txt = get_setting('tutorial_text') or 'How to get premium:'
        if video:
            bot.send_video(user_id, video, caption=txt)
        else:
            bot.send_message(user_id, txt)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_delete_tutorial":
        set_setting('tutorial_video', '')
        set_setting('tutorial_text', 'How to get premium:')
        bot.edit_message_text("Tutorial removed.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=admin_tutorial_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Payment settings
    if data == "admin_set_upi":
        user_data[user_id] = {'set': 'upi_id'}
        bot.edit_message_text("Enter UPI ID:", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_view_upi":
        upi = get_setting('upi_id') or 'Not set'
        bot.edit_message_text(f"UPI ID: {upi}", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_payment_settings_keyboard())
        bot.answer_callback_query(call.id)
        return

    if data == "admin_remove_upi":
        set_setting('upi_id', '')
        bot.edit_message_text("UPI removed.", chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=admin_payment_settings_keyboard())
        bot.answer_callback_query(call.id)
        return

    # Payments
    if data.startswith("view_payment_"):
        pid = int(data.split("_")[2])
        payment = get_payment(pid)
        if not payment:
            bot.answer_callback_query(call.id, "Not found")
            return
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT username, first_name FROM users WHERE user_id = ?', (payment['user_id'],))
        row = c.fetchone()
        conn.close()
        user = dict(row) if row else {}
        text = (f"Payment #{pid}\nUser: {user.get('username') or user.get('first_name')}\n"
                f"Plan: {payment['plan_name']} - ₹{payment['amount']} - {payment['validity']}d\n"
                f"Status: {payment['status']}\nDate: {payment['created_at']}")
        kb = types.InlineKeyboardMarkup(row_width=1)
        if payment['status'] == 'pending':
            kb.add(types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pid}"))
            kb.add(types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{pid}"))
        kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_payments"))
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=kb)
        if payment.get('screenshot_file_id'):
            bot.send_photo(user_id, payment['screenshot_file_id'], caption="Screenshot")
        bot.answer_callback_query(call.id)
        return

    if data.startswith("approve_"):
        pid = int(data.split("_")[1])
        payment = get_payment(pid)
        if not payment or payment['status'] != 'pending':
            bot.answer_callback_query(call.id, "Not pending")
            return
        update_payment_status(pid, 'approved')
        until = datetime.now() + timedelta(days=payment['validity'])
        update_user_premium(payment['user_id'], until)
        try:
            bot.send_message(payment['user_id'],
                             f"✅ Payment Approved!\nPlan: {payment['plan_name']}\nValid until: {until.strftime('%Y-%m-%d %H:%M')}")
        except:
            pass
        bot.edit_message_text(f"Payment #{pid} approved.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("🔙 Back", callback_data="admin_payments")))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("reject_"):
        pid = int(data.split("_")[1])
        payment = get_payment(pid)
        if not payment or payment['status'] != 'pending':
            bot.answer_callback_query(call.id, "Not pending")
            return
        update_payment_status(pid, 'rejected')
        try:
            bot.send_message(payment['user_id'], "❌ Payment rejected. Contact admin.")
        except:
            pass
        bot.edit_message_text(f"Payment #{pid} rejected.", chat_id=call.message.chat.id,
                              message_id=call.message.message_id,
                              reply_markup=types.InlineKeyboardMarkup().add(
                                  types.InlineKeyboardButton("🔙 Back", callback_data="admin_payments")))
        bot.answer_callback_query(call.id)
        return

# ---------- MESSAGE HANDLERS ----------
@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text

    # Admin setting updates
    if user_id == ADMIN_ID and 'set' in user_data.get(user_id, {}):
        key = user_data[user_id]['set']
        if key == 'upi_id':
            set_setting('upi_id', text)
            bot.reply_to(message, f"✅ UPI set to {text}")
        elif key == 'welcome_text':
            set_setting('welcome_text', text)
            bot.reply_to(message, "✅ Welcome text updated")
        elif key == 'tutorial_text':
            set_setting('tutorial_text', text)
            bot.reply_to(message, "✅ Tutorial text updated")
        del user_data[user_id]['set']
        return

    # Admin Add Plan
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('add_plan'):
        step = user_data[user_id].get('step')
        if step == 'name':
            user_data[user_id]['name'] = text
            user_data[user_id]['step'] = 'price'
            bot.reply_to(message, "💰 Enter price:")
        elif step == 'price':
            try:
                user_data[user_id]['price'] = int(text)
                user_data[user_id]['step'] = 'validity'
                bot.reply_to(message, "⏳ Enter validity (days):")
            except:
                bot.reply_to(message, "Invalid number. Enter price:")
        elif step == 'validity':
            try:
                user_data[user_id]['validity'] = int(text)
                user_data[user_id]['step'] = 'link'
                bot.reply_to(message, "🔗 Enter link (or 'skip'):")
            except:
                bot.reply_to(message, "Invalid days. Enter number:")
        elif step == 'link':
            link = text if text.lower() != 'skip' else ''
            user_data[user_id]['link'] = link
            # No image step – directly create plan
            name = user_data[user_id]['name']
            price = user_data[user_id]['price']
            validity = user_data[user_id]['validity']
            add_plan(name, price, validity, link)
            bot.reply_to(message, f"✅ Plan '{name}' added!")
            del user_data[user_id]
        return

    # Admin Edit Plan
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('edit_plan'):
        plan_id = user_data[user_id]['edit_plan']
        field = user_data[user_id]['field']
        if field == 'name':
            update_plan(plan_id, name=text)
            bot.reply_to(message, "✅ Name updated")
        elif field == 'price':
            try:
                update_plan(plan_id, price=int(text))
                bot.reply_to(message, "✅ Price updated")
            except:
                bot.reply_to(message, "Invalid number")
        elif field == 'validity':
            try:
                update_plan(plan_id, validity=int(text))
                bot.reply_to(message, "✅ Validity updated")
            except:
                bot.reply_to(message, "Invalid number")
        elif field == 'link':
            update_plan(plan_id, link=text if text.lower() != 'skip' else '')
            bot.reply_to(message, "✅ Link updated")
        elif field == 'desc':
            update_plan(plan_id, description=text)
            bot.reply_to(message, "✅ Description updated")
        del user_data[user_id]
        return

    # Broadcast
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('broadcast'):
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_message(u['user_id'], text)
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(message, f"✅ Broadcast sent to {sent} users")
        del user_data[user_id]['broadcast']
        return

    # Payment screenshot (user)
    if user_id in user_data and 'payment_plan' in user_data[user_id]:
        bot.reply_to(message, "Please send a photo as payment proof.")
        return

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    file_id = message.photo[-1].file_id

    # Admin set image (welcome or plans)
    if user_id == ADMIN_ID and 'set' in user_data.get(user_id, {}):
        key = user_data[user_id]['set']
        if key in ('welcome_image', 'plans_image'):
            set_setting(key, file_id)
            bot.reply_to(message, f"✅ {key.replace('_', ' ').title()} updated")
            del user_data[user_id]['set']
            return

    # Admin add demo media
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('add_demo'):
        add_demo_media('photo', file_id)
        bot.reply_to(message, "✅ Photo added. Send more or press Done.")
        return

    # Payment screenshot
    if user_id in user_data and 'payment_plan' in user_data[user_id]:
        plan_id = user_data[user_id]['payment_plan']
        plan = get_plan(plan_id)
        if not plan:
            bot.reply_to(message, "Plan not found")
            return
        payment_id = add_payment(user_id, plan_id, plan['name'], plan['price'], plan['validity'], file_id)
        bot.reply_to(message, "✅ Payment request submitted! Admin will review.")
        bot.send_photo(ADMIN_ID, file_id, caption=f"New payment #{payment_id}")
        del user_data[user_id]['payment_plan']
        return

    # Broadcast photo
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('broadcast'):
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_photo(u['user_id'], file_id, caption=message.caption or '')
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(message, f"✅ Broadcast sent to {sent} users")
        del user_data[user_id]['broadcast']
        return

@bot.message_handler(content_types=['video'])
def handle_video(message):
    user_id = message.from_user.id
    file_id = message.video.file_id

    # Admin set tutorial video
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('set') == 'tutorial_video':
        set_setting('tutorial_video', file_id)
        if message.caption:
            set_setting('tutorial_text', message.caption)
        bot.reply_to(message, "✅ Tutorial video updated")
        del user_data[user_id]['set']
        return

    # Admin add demo video
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('add_demo'):
        add_demo_media('video', file_id)
        bot.reply_to(message, "✅ Video added. Send more or press Done.")
        return

    # Broadcast video
    if user_id == ADMIN_ID and user_data.get(user_id, {}).get('broadcast'):
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_video(u['user_id'], file_id, caption=message.caption or '')
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(message, f"✅ Broadcast sent to {sent} users")
        del user_data[user_id]['broadcast']
        return

# ---------- POLLING ----------
if __name__ == "__main__":
    logger.info("✅ Handlers registered")
    logger.info("🤖 Telegram connected...")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        logger.exception("Polling error")