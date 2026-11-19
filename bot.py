import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime
import psutil
import sqlite3
import threading
import sys
import io
import requests
from flask import Flask
from threading import Thread

# ==========================================
# ✅ FLASK KEEP ALIVE (Railway Web Service)
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Hosting Bot is Alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ==========================================
# ✅ CONFIGURATION (Environment Variables)
# ==========================================
TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
OWNER_ID = int(os.environ.get('OWNER_ID', '123456789'))
ADMIN_ID = OWNER_ID

YOUR_USERNAME = '@YourUsername'
UPDATE_CHANNEL = 'https://t.me/YourChannel'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.environ.get('UPLOAD_BOTS_DIR', os.path.join(BASE_DIR, 'upload_bots'))
IROTECH_DIR = os.environ.get('IROTECH_DIR', os.path.join(BASE_DIR, 'inf'))
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

FREE_USER_LIMIT = 5
SUBSCRIBED_USER_LIMIT = 20
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
DB_LOCK = threading.Lock()

FILE_STATUS_PENDING = "pending"
FILE_STATUS_APPROVED = "approved"
FILE_STATUS_REJECTED = "rejected"

# ==========================================
# ✅ DATABASE SETUP
# ==========================================
def init_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_files (user_id INTEGER, file_name TEXT, file_type TEXT, PRIMARY KEY (user_id, file_name))''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS file_approvals
                 (user_id INTEGER, file_name TEXT, status TEXT, 
                  reviewed_by INTEGER, review_time TEXT, file_type TEXT,
                  uploaded_time TEXT, message_id INTEGER,
                  PRIMARY KEY (user_id, file_name))''')
    c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
    conn.commit()
    conn.close()

def load_data():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('SELECT user_id FROM active_users')
    active_users.update(user_id for (user_id,) in c.fetchall())
    c.execute('SELECT user_id FROM admins')
    admin_ids.update(user_id for (user_id,) in c.fetchall())
    c.execute('SELECT user_id, file_name, file_type FROM user_files')
    for user_id, file_name, file_type in c.fetchall():
        if user_id not in user_files: user_files[user_id] = []
        user_files[user_id].append((file_name, file_type))
    conn.close()

init_db()
load_data()

# ==========================================
# ✅ HELPER FUNCTIONS
# ==========================================
def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id): return len(user_files.get(user_id, []))

def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    if script_info and script_info.get('process'):
        try:
            proc = psutil.Process(script_info['process'].pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE: return True
            else: kill_process_tree(script_info); return False
        except psutil.NoSuchProcess:
            kill_process_tree(script_info); return False
    return False

def kill_process_tree(process_info):
    try:
        if 'log_file' in process_info and not process_info['log_file'].closed: process_info['log_file'].close()
        process = process_info.get('process')
        if process:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try: child.terminate()
                except: pass
            parent.terminate()
            try: parent.wait(timeout=2)
            except: parent.kill()
    except: pass

def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)', (user_id, file_name, file_type))
        conn.commit()
        conn.close()
        if user_id not in user_files: user_files[user_id] = []
        user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != file_name]
        user_files[user_id].append((file_name, file_type))

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        c.execute('DELETE FROM file_approvals WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        conn.commit()
        conn.close()
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]

def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
        conn.commit()
        conn.close()

def get_file_status(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT status, reviewed_by, review_time, file_type FROM file_approvals WHERE user_id=? AND file_name=?', (user_id, file_name))
        result = c.fetchone()
        conn.close()
        if result: return {'status': result[0], 'reviewed_by': result[1], 'review_time': result[2], 'file_type': result[3]}
        return {'status': FILE_STATUS_PENDING, 'file_type': 'unknown'}

def update_file_status(user_id, file_name, status, admin_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('UPDATE file_approvals SET status=?, reviewed_by=?, review_time=? WHERE user_id=? AND file_name=?', (status, admin_id, datetime.now().isoformat(), user_id, file_name))
        conn.commit()
        conn.close()

def save_file_approval(user_id, file_name, file_type, status=FILE_STATUS_PENDING, reviewed_by=None):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO file_approvals (user_id, file_name, file_type, status, reviewed_by, review_time, uploaded_time) VALUES (?, ?, ?, ?, ?, ?, ?)''', (user_id, file_name, file_type, status, reviewed_by, datetime.now().isoformat() if reviewed_by else None, datetime.now().isoformat()))
        conn.commit()
        conn.close()

def send_file_for_approval(message, user_id, file_name, file_type):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("✅ Approve", callback_data=f'approve_{user_id}_{file_name}'), types.InlineKeyboardButton("❌ Reject", callback_data=f'reject_{user_id}_{file_name}'))
    for admin_id in admin_ids:
        try: bot.send_message(admin_id, f"📄 **NEW FILE**\n👤 User: `{user_id}`\n📁 File: `{file_name}`\n📊 Type: {file_type}", reply_markup=markup, parse_mode='Markdown')
        except: pass

# ==========================================
# ✅ SCRIPT RUNNERS
# ==========================================
def run_script(script_path, script_owner_id, user_folder, file_name, message_obj):
    if get_file_status(script_owner_id, file_name)['status'] != FILE_STATUS_APPROVED:
        bot.reply_to(message_obj, f"❌ File not approved yet!"); return
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file = open(os.path.join(user_folder, f"{file_name}.log"), 'a')
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder, stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {'process': process, 'log_file': log_file, 'file_name': file_name, 'script_owner_id': script_owner_id, 'user_folder': user_folder}
        bot.reply_to(message_obj, f"✅ Script started! (PID: {process.pid})")
    except Exception as e: bot.reply_to(message_obj, f"❌ Error: {e}")

def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj):
    if get_file_status(script_owner_id, file_name)['status'] != FILE_STATUS_APPROVED:
        bot.reply_to(message_obj, f"❌ File not approved yet!"); return
    script_key = f"{script_owner_id}_{file_name}"
    try:
        log_file = open(os.path.join(user_folder, f"{file_name}.log"), 'a')
        process = subprocess.Popen(['node', script_path], cwd=user_folder, stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {'process': process, 'log_file': log_file, 'file_name': file_name, 'script_owner_id': script_owner_id, 'user_folder': user_folder}
        bot.reply_to(message_obj, f"✅ JS Script started! (PID: {process.pid})")
    except Exception as e: bot.reply_to(message_obj, f"❌ Error: {e}")

# ==========================================
# ✅ MENUS
# ==========================================
def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton('📢 Updates', url=UPDATE_CHANNEL), types.InlineKeyboardButton('📤 Upload File', callback_data='upload'))
    markup.add(types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'), types.InlineKeyboardButton('📊 Statistics', callback_data='stats'))
    if user_id in admin_ids:
        markup.add(types.InlineKeyboardButton('📋 Pending Files', callback_data='view_pending'))
    markup.add(types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'))
    return markup

def create_control_buttons(script_owner_id, file_name, is_running=True):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.row(types.InlineKeyboardButton("🔴 Stop", callback_data=f'stop_{script_owner_id}_{file_name}'), types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}'))
    else:
        markup.row(types.InlineKeyboardButton("🟢 Start", callback_data=f'start_{script_owner_id}_{file_name}'), types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{script_owner_id}_{file_name}'))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='check_files'))
    return markup

# ==========================================
# ✅ HANDLERS
# ==========================================
@bot.message_handler(commands=['start'])
def start_cmd(message):
    add_active_user(message.from_user.id)
    bot.send_message(message.chat.id, f"Welcome {message.from_user.first_name}!\nSend /upload to submit your scripts.", reply_markup=create_main_menu_inline(message.from_user.id))

@bot.message_handler(commands=['upload'])
def upload_cmd(message):
    bot.reply_to(message, "Send your `.py` or `.js` file. Admin approval is required.")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    user_id = message.from_user.id
    if get_user_file_count(user_id) >= get_user_file_limit(user_id):
        bot.reply_to(message, "⚠️ File limit reached."); return
    
    doc = message.document
    file_name = doc.file_name
    file_ext = os.path.splitext(file_name)[1].lower()
    if file_ext not in ['.py', '.js']:
        bot.reply_to(message, "❌ Only `.py` and `.js` allowed."); return

    msg = bot.reply_to(message, f"Downloading...")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        user_folder = get_user_folder(user_id)
        with open(os.path.join(user_folder, file_name), 'wb') as f: f.write(downloaded_file)
        
        save_user_file(user_id, file_name, 'py' if file_ext == '.py' else 'js')
        save_file_approval(user_id, file_name, 'py' if file_ext == '.py' else 'js')
        send_file_for_approval(message, user_id, file_name, 'py' if file_ext == '.py' else 'js')
        
        bot.edit_message_text(f"✅ Uploaded! Waiting for admin approval.", message.chat.id, msg.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {e}", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: True)
def handle_cb(call):
    user_id = call.from_user.id
    data = call.data

    if data == 'check_files':
        user_files_list = user_files.get(user_id, [])
        if not user_files_list: return bot.answer_callback_query(call.id, "No files.", show_alert=True)
        markup = types.InlineKeyboardMarkup(row_width=1)
        for file_name, file_type in user_files_list:
            status = get_file_status(user_id, file_name)['status']
            icon = "✅" if status == FILE_STATUS_APPROVED else "⏳"
            markup.add(types.InlineKeyboardButton(f"{icon} {file_name}", callback_data=f'file_{user_id}_{file_name}'))
        bot.edit_message_text("Your Files:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    
    elif data.startswith('file_'):
        _, s_id, f_name = data.split('_', 2)
        bot.edit_message_text(f"Controls:", call.message.chat.id, call.message.message_id, reply_markup=create_control_buttons(int(s_id), f_name, is_bot_running(int(s_id), f_name)))
    
    elif data.startswith('start_'):
        _, s_id, f_name = data.split('_', 2)
        if get_file_status(int(s_id), f_name)['status'] == FILE_STATUS_APPROVED:
            threading.Thread(target=run_script if f_name.endswith('.py') else run_js_script, args=(os.path.join(get_user_folder(int(s_id)), f_name), int(s_id), get_user_folder(int(s_id)), f_name, call.message)).start()
            bot.answer_callback_query(call.id, "Starting...")
        else: bot.answer_callback_query(call.id, "❌ Not approved!", show_alert=True)
    
    elif data.startswith('stop_'):
        _, s_id, f_name = data.split('_', 2)
        script_key = f"{s_id}_{f_name}"
        if script_key in bot_scripts: kill_process_tree(bot_scripts[script_key]); del bot_scripts[script_key]
        bot.edit_message_text("Stopped.", call.message.chat.id, call.message.message_id)
    
    elif data.startswith('delete_'):
        _, s_id, f_name = data.split('_', 2)
        remove_user_file_db(int(s_id), f_name)
        bot.edit_message_text("Deleted.", call.message.chat.id, call.message.message_id)
    
    elif data.startswith('approve_'):
        _, s_id, f_name = data.split('_', 2)
        update_file_status(int(s_id), f_name, FILE_STATUS_APPROVED, user_id)
        bot.edit_message_text(f"✅ Approved", call.message.chat.id, call.message.message_id)
        try: bot.send_message(int(s_id), f"✅ Your file `{f_name}` is approved!")
        except: pass
    
    elif data.startswith('reject_'):
        _, s_id, f_name = data.split('_', 2)
        update_file_status(int(s_id), f_name, FILE_STATUS_REJECTED, user_id)
        bot.edit_message_text(f"❌ Rejected", call.message.chat.id, call.message.message_id)

if __name__ == '__main__':
    keep_alive()
    bot.infinity_polling()
