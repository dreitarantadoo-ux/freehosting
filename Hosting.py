import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import signal
import threading
import re
import sys
import atexit
import requests
from flask import Flask
from threading import Thread

# ============================================================
#  CONFIG LOADER
# ============================================================
BASE_DIR   = os.path.abspath(os.path.dirname(__file__))
DATA_DIR   = os.path.join(BASE_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')

os.makedirs(DATA_DIR, exist_ok=True)

def load_config():
    if not os.path.exists(CONFIG_PATH):
        default = {
            "bot_token": "YOUR_BOT_TOKEN_HERE",
            "owner_id": 0,
            "admin_ids": [],
            "your_username": "@yourusername",
            "update_channel": "https://t.me/yourchannel",
            "limits": {"free_user": 2, "subscribed_user": 15, "admin": 999},
            "max_file_size_mb": 20,
            "allowed_extensions": [".py", ".js", ".zip"]
        }
        with open(CONFIG_PATH, 'w') as f:
            json.dump(default, f, indent=4)
        print(f"[WARN] config.json not found. Created default at {CONFIG_PATH}. Fill it in and restart.")
        sys.exit(1)
    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)
    return cfg

CFG = load_config()

TOKEN          = CFG['bot_token']
OWNER_ID       = int(CFG['owner_id'])
YOUR_USERNAME  = CFG.get('your_username', '@owner')
UPDATE_CHANNEL = CFG.get('update_channel', 'https://t.me/')
LIMITS         = CFG.get('limits', {"free_user": 2, "subscribed_user": 15, "admin": 999})
MAX_FILE_MB    = CFG.get('max_file_size_mb', 20)
ALLOWED_EXT    = CFG.get('allowed_extensions', ['.py', '.js', '.zip'])

FREE_USER_LIMIT       = LIMITS.get('free_user', 2)
SUBSCRIBED_USER_LIMIT = LIMITS.get('subscribed_user', 15)
ADMIN_LIMIT           = LIMITS.get('admin', 999)
OWNER_LIMIT           = float('inf')

# ============================================================
#  DIRECTORIES & LOGGING
# ============================================================
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
DATABASE_PATH   = os.path.join(DATA_DIR, 'bot_data.db')
PENDING_DIR     = os.path.join(DATA_DIR, 'pending_uploads')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(PENDING_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DATA_DIR, 'bot.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
#  FLASK (RAILWAY PORT BINDING)
# ============================================================
app = Flask('')

@app.route('/')
def home():
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime = get_uptime()
    return (
        f"<h2>🤖 Hosting Bot — Running</h2>"
        f"<p>Uptime: {uptime}</p>"
        f"<p>CPU: {cpu}%</p>"
        f"<p>RAM: {ram.percent}% ({_fmt_bytes(ram.used)} / {_fmt_bytes(ram.total)})</p>"
        f"<p>Disk: {disk.percent}% ({_fmt_bytes(disk.used)} / {_fmt_bytes(disk.total)})</p>"
    )

@app.route('/health')
def health():
    return {"status": "ok", "uptime": get_uptime()}, 200

def _fmt_bytes(b):
    for u in ['B','KB','MB','GB']:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info(f"Flask server started on port {os.environ.get('PORT', 8080)}")

# ============================================================
#  BOT INIT
# ============================================================
BOT_START_TIME = datetime.now()

def get_uptime():
    d = datetime.now() - BOT_START_TIME
    days = d.days
    h, rem = divmod(d.seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{days}d {h}h {m}m {s}s"

bot = telebot.TeleBot(TOKEN, parse_mode=None)

bot_scripts        = {}
user_subscriptions = {}
user_files         = {}
active_users       = set()
admin_ids          = set(int(i) for i in CFG.get('admin_ids', []))
admin_ids.add(OWNER_ID)
bot_locked         = False
DB_LOCK            = threading.Lock()

# pending_uploads[pending_id] = {user_id, file_name, file_path, file_type, timestamp}
pending_uploads = {}

# ============================================================
#  DATABASE
# ============================================================
def init_db():
    logger.info(f"Initializing DB at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pending_uploads
                     (pending_id TEXT PRIMARY KEY, user_id INTEGER,
                      file_name TEXT, file_path TEXT, file_type TEXT,
                      timestamp TEXT)''')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        for aid in CFG.get('admin_ids', []):
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (int(aid),))
        conn.commit()
        conn.close()
        logger.info("DB initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data from DB...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for uid, exp in c.fetchall():
            try: user_subscriptions[uid] = {'expiry': datetime.fromisoformat(exp)}
            except ValueError: pass
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for uid, fn, ft in c.fetchall():
            user_files.setdefault(uid, []).append((fn, ft))
        c.execute('SELECT user_id FROM active_users')
        active_users.update(r[0] for r in c.fetchall())
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(r[0] for r in c.fetchall())
        c.execute('SELECT pending_id, user_id, file_name, file_path, file_type, timestamp FROM pending_uploads')
        for pid, uid, fn, fp, ft, ts in c.fetchall():
            pending_uploads[pid] = {
                'user_id': uid, 'file_name': fn, 'file_path': fp,
                'file_type': ft, 'timestamp': ts
            }
        conn.close()
        logger.info(f"Loaded: {len(active_users)} users, {len(admin_ids)} admins, {len(pending_uploads)} pending.")
    except Exception as e:
        logger.error(f"Load data error: {e}", exc_info=True)

init_db()
load_data()

# ============================================================
#  DB HELPERS
# ============================================================
def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?,?,?)',
                      (user_id, file_name, file_type))
            conn.commit()
            user_files.setdefault(user_id, [])
            user_files[user_id] = [(fn,ft) for fn,ft in user_files[user_id] if fn != file_name]
            user_files[user_id].append((file_name, file_type))
        except Exception as e: logger.error(f"save_user_file error: {e}")
        finally: conn.close()

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (user_id, file_name))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
                if not user_files[user_id]: del user_files[user_id]
        except Exception as e: logger.error(f"remove_user_file error: {e}")
        finally: conn.close()

def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
        except Exception as e: logger.error(f"add_active_user error: {e}")
        finally: conn.close()

def save_subscription(user_id, expiry):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?,?)',
                      (user_id, expiry.isoformat()))
            conn.commit()
            user_subscriptions[user_id] = {'expiry': expiry}
        except Exception as e: logger.error(f"save_subscription error: {e}")
        finally: conn.close()

def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
            conn.commit()
            user_subscriptions.pop(user_id, None)
        except Exception as e: logger.error(f"remove_subscription error: {e}")
        finally: conn.close()

def add_admin_db(admin_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (admin_id,))
            conn.commit()
            admin_ids.add(admin_id)
        except Exception as e: logger.error(f"add_admin error: {e}")
        finally: conn.close()

def remove_admin_db(admin_id):
    if admin_id == OWNER_ID: return False
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM admins WHERE user_id=?', (admin_id,))
            conn.commit()
            admin_ids.discard(admin_id)
            return True
        except Exception as e: logger.error(f"remove_admin error: {e}"); return False
        finally: conn.close()

def save_pending_upload(pending_id, user_id, file_name, file_path, file_type):
    ts = datetime.now().isoformat()
    pending_uploads[pending_id] = {
        'user_id': user_id, 'file_name': file_name, 'file_path': file_path,
        'file_type': file_type, 'timestamp': ts
    }
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO pending_uploads VALUES (?,?,?,?,?,?)',
                      (pending_id, user_id, file_name, file_path, file_type, ts))
            conn.commit()
        except Exception as e: logger.error(f"save_pending error: {e}")
        finally: conn.close()

def remove_pending_upload(pending_id):
    pending_uploads.pop(pending_id, None)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM pending_uploads WHERE pending_id=?', (pending_id,))
            conn.commit()
        except Exception as e: logger.error(f"remove_pending error: {e}")
        finally: conn.close()

# ============================================================
#  USER HELPERS
# ============================================================
def get_user_folder(user_id):
    p = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(p, exist_ok=True)
    return p

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

def get_user_level(user_id):
    if user_id == OWNER_ID: return "👑 Owner"
    if user_id in admin_ids: return "🛡 Admin"
    if user_id in user_subscriptions and user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now():
        return "⭐ Premium"
    return "👤 Free User"

# ============================================================
#  SYSTEM STATS
# ============================================================
def get_system_stats():
    cpu  = psutil.cpu_percent(interval=0.5)
    ram  = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net  = psutil.net_io_counters()
    boot = datetime.fromtimestamp(psutil.boot_time())
    sys_uptime = datetime.now() - boot
    days = sys_uptime.days
    h, rem = divmod(sys_uptime.seconds, 3600)
    m, s = divmod(rem, 60)
    sys_up_str = f"{days}d {h}h {m}m {s}s"
    return {
        'cpu': cpu,
        'ram_used': _fmt_bytes(ram.used),
        'ram_total': _fmt_bytes(ram.total),
        'ram_pct': ram.percent,
        'disk_used': _fmt_bytes(disk.used),
        'disk_total': _fmt_bytes(disk.total),
        'disk_pct': disk.percent,
        'net_sent': _fmt_bytes(net.bytes_sent),
        'net_recv': _fmt_bytes(net.bytes_recv),
        'sys_uptime': sys_up_str,
        'cpu_count': psutil.cpu_count(),
        'load_avg': os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
    }

def format_system_stats():
    s = get_system_stats()
    return (
        f"📊 *System Resources*\n\n"
        f"🖥 *CPU:* {s['cpu']}% (cores: {s['cpu_count']})\n"
        f"   Load avg: {s['load_avg'][0]:.2f} / {s['load_avg'][1]:.2f} / {s['load_avg'][2]:.2f}\n\n"
        f"🧠 *RAM:* {s['ram_pct']}%\n"
        f"   {s['ram_used']} / {s['ram_total']}\n\n"
        f"💾 *Disk:* {s['disk_pct']}%\n"
        f"   {s['disk_used']} / {s['disk_total']}\n\n"
        f"🌐 *Network:*\n"
        f"   ↑ Sent: {s['net_sent']}  ↓ Recv: {s['net_recv']}\n\n"
        f"⏱ *System Uptime:* {s['sys_uptime']}\n"
        f"🤖 *Bot Uptime:* {get_uptime()}"
    )

# ============================================================
#  PROCESS MANAGEMENT
# ============================================================
def is_bot_running(owner_id, file_name):
    key = f"{owner_id}_{file_name}"
    info = bot_scripts.get(key)
    if not info or not info.get('process'): return False
    try:
        proc = psutil.Process(info['process'].pid)
        running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        if not running:
            _close_log(info)
            bot_scripts.pop(key, None)
        return running
    except psutil.NoSuchProcess:
        _close_log(info); bot_scripts.pop(key, None); return False
    except Exception: return False

def _close_log(info):
    lf = info.get('log_file')
    if lf and not lf.closed:
        try: lf.close()
        except: pass

def kill_process_tree(info):
    _close_log(info)
    proc = info.get('process')
    if not proc: return
    pid = getattr(proc, 'pid', None)
    if not pid: return
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for ch in children:
            try: ch.terminate()
            except psutil.NoSuchProcess: pass
        gone, alive = psutil.wait_procs(children, timeout=2)
        for p in alive:
            try: p.kill()
            except: pass
        try: parent.terminate(); parent.wait(timeout=2)
        except psutil.NoSuchProcess: pass
        except psutil.TimeoutExpired:
            try: parent.kill()
            except: pass
    except psutil.NoSuchProcess: pass
    except Exception as e: logger.error(f"kill_process_tree error: {e}")

# ============================================================
#  MODULE DETECTION & AUTO-INSTALL
# ============================================================
TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI', 'telegram': 'python-telegram-bot',
    'aiogram': 'aiogram', 'pyrogram': 'pyrogram', 'telethon': 'telethon',
    'telepot': 'telepot', 'tgcrypto': 'tgcrypto',
}

def detect_imports(script_path):
    imports = []
    try:
        with open(script_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                m = re.match(r'^import\s+([\w.]+)', line)
                if m: imports.append(m.group(1).split('.')[0])
                m = re.match(r'^from\s+([\w.]+)\s+import', line)
                if m: imports.append(m.group(1).split('.')[0])
    except Exception as e: logger.error(f"detect_imports error: {e}")
    return imports

def attempt_install_pip(module_name, msg_obj):
    pkg = TELEGRAM_MODULES.get(module_name, module_name)
    bot.send_message(msg_obj.chat.id, f"📦 Auto-installing `{pkg}`...", parse_mode='Markdown')
    try:
        r = subprocess.run([sys.executable, '-m', 'pip', 'install', pkg, '--quiet'],
                           capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            bot.send_message(msg_obj.chat.id, f"✅ Installed `{pkg}`.", parse_mode='Markdown')
            return True
        else:
            bot.send_message(msg_obj.chat.id, f"❌ Failed to install `{pkg}`:\n```{r.stderr[:300]}```", parse_mode='Markdown')
            return False
    except subprocess.TimeoutExpired:
        bot.send_message(msg_obj.chat.id, f"⏰ Install timeout for `{pkg}`.", parse_mode='Markdown')
        return False
    except Exception as e:
        logger.error(f"pip install error {pkg}: {e}")
        return False

def attempt_install_npm(module_name, user_folder, msg_obj):
    bot.send_message(msg_obj.chat.id, f"📦 npm installing `{module_name}`...", parse_mode='Markdown')
    try:
        r = subprocess.run(['npm', 'install', module_name, '--save', '--quiet'],
                           cwd=user_folder, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            bot.send_message(msg_obj.chat.id, f"✅ npm installed `{module_name}`.", parse_mode='Markdown')
            return True
        else:
            bot.send_message(msg_obj.chat.id, f"❌ npm install failed for `{module_name}`.", parse_mode='Markdown')
            return False
    except Exception as e:
        logger.error(f"npm install error {module_name}: {e}")
        return False

# ============================================================
#  SCRIPT RUNNERS
# ============================================================
def run_script(script_path, owner_id, user_folder, file_name, msg_obj, attempt=1):
    MAX_ATTEMPTS = 2
    if attempt > MAX_ATTEMPTS:
        bot.reply_to(msg_obj, f"❌ Failed to run `{file_name}` after {MAX_ATTEMPTS} attempts.")
        return
    script_key = f"{owner_id}_{file_name}"
    if is_bot_running(owner_id, file_name):
        bot.reply_to(msg_obj, f"⚠️ `{file_name}` is already running!")
        return
    if not os.path.exists(script_path):
        bot.reply_to(msg_obj, f"❌ File `{file_name}` not found.")
        remove_user_file_db(owner_id, file_name)
        return
    if attempt == 1:
        check = None
        try:
            check = subprocess.Popen([sys.executable, script_path], cwd=user_folder,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='ignore')
            _, stderr = check.communicate(timeout=5)
            if check.returncode and check.returncode != 0 and stderr:
                m = re.search(r"No module named '([^']+)'", stderr)
                if m:
                    mod = m.group(1).split('.')[0]
                    if attempt_install_pip(mod, msg_obj):
                        bot.reply_to(msg_obj, f"🔄 Retrying `{file_name}`...")
                        time.sleep(2)
                        threading.Thread(target=run_script, args=(
                            script_path, owner_id, user_folder, file_name, msg_obj, attempt+1)).start()
                        return
                    else:
                        bot.reply_to(msg_obj, f"❌ Cannot run `{file_name}`: missing module `{mod}`.")
                        return
                bot.reply_to(msg_obj, f"❌ Script error:\n```\n{stderr[:500]}\n```", parse_mode='Markdown')
                return
        except subprocess.TimeoutExpired:
            if check and check.poll() is None: check.kill(); check.communicate()
        except Exception as e:
            bot.reply_to(msg_obj, f"❌ Pre-check error: {e}")
            return
        finally:
            if check and check.poll() is None: check.kill(); check.communicate()

    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
    except Exception as e:
        bot.reply_to(msg_obj, f"❌ Cannot open log: {e}")
        return
    try:
        process = subprocess.Popen(
            [sys.executable, script_path], cwd=user_folder,
            stdout=log_file, stderr=log_file, stdin=subprocess.PIPE,
            encoding='utf-8', errors='ignore'
        )
        bot_scripts[script_key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'chat_id': msg_obj.chat.id, 'script_owner_id': owner_id,
            'start_time': datetime.now(), 'user_folder': user_folder,
            'type': 'py', 'script_key': script_key
        }
        bot.reply_to(msg_obj, f"✅ `{file_name}` started! PID: `{process.pid}`", parse_mode='Markdown')
    except Exception as e:
        _close_log({'log_file': log_file})
        bot.reply_to(msg_obj, f"❌ Error starting `{file_name}`: {e}")

def run_js_script(script_path, owner_id, user_folder, file_name, msg_obj, attempt=1):
    MAX_ATTEMPTS = 2
    if attempt > MAX_ATTEMPTS:
        bot.reply_to(msg_obj, f"❌ Failed to run `{file_name}` after {MAX_ATTEMPTS} attempts.")
        return
    script_key = f"{owner_id}_{file_name}"
    if is_bot_running(owner_id, file_name):
        bot.reply_to(msg_obj, f"⚠️ `{file_name}` is already running!")
        return
    if not os.path.exists(script_path):
        bot.reply_to(msg_obj, f"❌ `{file_name}` not found.")
        remove_user_file_db(owner_id, file_name)
        return
    if attempt == 1:
        check = None
        try:
            check = subprocess.Popen(['node', script_path], cwd=user_folder,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='ignore')
            _, stderr = check.communicate(timeout=5)
            if check.returncode and check.returncode != 0 and stderr:
                m = re.search(r"Cannot find module '(.+?)'", stderr)
                if m:
                    mod = m.group(1).strip().strip("'\"")
                    if not mod.startswith('.') and not mod.startswith('/'):
                        if attempt_install_npm(mod, user_folder, msg_obj):
                            bot.reply_to(msg_obj, f"🔄 Retrying `{file_name}`...")
                            time.sleep(2)
                            threading.Thread(target=run_js_script, args=(
                                script_path, owner_id, user_folder, file_name, msg_obj, attempt+1)).start()
                            return
                bot.reply_to(msg_obj, f"❌ JS error:\n```\n{stderr[:500]}\n```", parse_mode='Markdown')
                return
        except subprocess.TimeoutExpired:
            if check and check.poll() is None: check.kill(); check.communicate()
        except FileNotFoundError:
            bot.reply_to(msg_obj, "❌ Node.js not found. Install Node.js on the server.")
            return
        except Exception as e:
            bot.reply_to(msg_obj, f"❌ JS pre-check error: {e}")
            return
        finally:
            if check and check.poll() is None: check.kill(); check.communicate()

    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
    except Exception as e:
        bot.reply_to(msg_obj, f"❌ Cannot open log: {e}")
        return
    try:
        process = subprocess.Popen(
            ['node', script_path], cwd=user_folder,
            stdout=log_file, stderr=log_file, stdin=subprocess.PIPE,
            encoding='utf-8', errors='ignore'
        )
        bot_scripts[script_key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'chat_id': msg_obj.chat.id, 'script_owner_id': owner_id,
            'start_time': datetime.now(), 'user_folder': user_folder,
            'type': 'js', 'script_key': script_key
        }
        bot.reply_to(msg_obj, f"✅ `{file_name}` (JS) started! PID: `{process.pid}`", parse_mode='Markdown')
    except FileNotFoundError:
        _close_log({'log_file': log_file})
        bot.reply_to(msg_obj, "❌ Node.js not found for long run.")
    except Exception as e:
        _close_log({'log_file': log_file})
        bot.reply_to(msg_obj, f"❌ Error starting `{file_name}`: {e}")

# ============================================================
#  ZIP HANDLER
# ============================================================
def _launch_entry_point(user_id, user_folder, entry_base, all_bases, message):
    """Start only the chosen entry point; save all extracted files."""
    for base in all_bases:
        ft = 'py' if base.endswith('.py') else 'js'
        sk = f"{user_id}_{base}"
        if is_bot_running(user_id, base):
            kill_process_tree(bot_scripts[sk])
            del bot_scripts[sk]
            time.sleep(0.3)
        save_user_file(user_id, base, ft)

    fp = os.path.join(user_folder, entry_base)
    ft = 'py' if entry_base.endswith('.py') else 'js'
    if os.path.exists(fp):
        if ft == 'py':
            threading.Thread(target=run_script,
                             args=(fp, user_id, user_folder, entry_base, message)).start()
        else:
            threading.Thread(target=run_js_script,
                             args=(fp, user_id, user_folder, entry_base, message)).start()


def handle_zip_file(content_bytes, zip_name, message):
    user_id = message.from_user.id
    user_folder = get_user_folder(user_id)
    tmp_zip = os.path.join(user_folder, zip_name)
    with open(tmp_zip, 'wb') as f:
        f.write(content_bytes)
    try:
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            py_files = [n for n in zf.namelist() if n.endswith('.py') and not n.startswith('__')]
            js_files = [n for n in zf.namelist() if n.endswith('.js')]
            if not py_files and not js_files:
                bot.reply_to(message, "❌ No .py or .js files found in zip.")
                os.remove(tmp_zip)
                return
            zf.extractall(user_folder)
        os.remove(tmp_zip)

        script_files = py_files + js_files
        all_bases = []
        for fn in script_files:
            base = os.path.basename(fn)
            src  = os.path.join(user_folder, fn)
            dst  = os.path.join(user_folder, base)
            if src != dst and os.path.exists(src):
                shutil.move(src, dst)
            all_bases.append(base)

        names_str = '\n'.join(
            f"  {'🐍' if b.endswith('.py') else '🟨'} `{b}`"
            for b in all_bases
        )

        # ── Single script → auto-run, no question asked ──────────────────
        if len(all_bases) == 1:
            entry = all_bases[0]
            zip_card = (
                "╔══════════════════════════════╗\n"
                "║      ✅  ZIP EXTRACTED        ║\n"
                "╚══════════════════════════════╝\n\n"
                f"📦 *Zip:* `{zip_name}`\n"
                f"📄 *File:* `{entry}`\n\n"
                "▶️ Auto-starting..."
            )
            mk = types.InlineKeyboardMarkup(row_width=1)
            mk.add(types.InlineKeyboardButton('📂 My Files', callback_data='check_files'))
            bot.reply_to(message, zip_card, parse_mode='Markdown', reply_markup=mk)
            _launch_entry_point(user_id, user_folder, entry, all_bases, message)
            return

        # ── Multiple scripts → ask which one is the entry point ──────────
        pick_text = (
            "╔══════════════════════════════╗\n"
            "║      📦  ZIP EXTRACTED        ║\n"
            "╚══════════════════════════════╝\n\n"
            f"*{len(all_bases)} scripts found inside* `{zip_name}`:\n\n"
            f"{names_str}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ *Which file should be the entry point?*\n"
            "Tap to select — only that file will be started."
        )
        mk = types.InlineKeyboardMarkup(row_width=1)
        for base in all_bases:
            icon = '🐍' if base.endswith('.py') else '🟨'
            mk.add(types.InlineKeyboardButton(
                f"{icon} {base}",
                callback_data=f"entrypoint_{user_id}_{base}"
            ))
        bot.reply_to(message, pick_text, parse_mode='Markdown', reply_markup=mk)

        # Store context so callback can launch it
        if not hasattr(bot, '_zip_contexts'):
            bot._zip_contexts = {}
        bot._zip_contexts[f"{user_id}_{zip_name}"] = {
            'all_bases': all_bases,
            'user_folder': user_folder,
            'message': message,
        }

    except zipfile.BadZipFile:
        bot.reply_to(message, "❌ Invalid zip file.")
        if os.path.exists(tmp_zip): os.remove(tmp_zip)
    except Exception as e:
        bot.reply_to(message, f"❌ Zip error: {e}")
        logger.error(f"Zip error: {e}", exc_info=True)

# ============================================================
#  INLINE KEYBOARDS
# ============================================================
def create_main_menu_inline(user_id):
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton('📢 Channel', url=UPDATE_CHANNEL),
        types.InlineKeyboardButton('📤 Upload', callback_data='upload'),
        types.InlineKeyboardButton('📂 My Files', callback_data='check_files'),
        types.InlineKeyboardButton('📁 See All Files', callback_data='see_all_files'),
        types.InlineKeyboardButton('📊 Stats', callback_data='stats'),
        types.InlineKeyboardButton('⚡ Speed', callback_data='speed'),
        types.InlineKeyboardButton('💻 System', callback_data='sysinfo'),
        types.InlineKeyboardButton('⏱ Uptime', callback_data='uptime'),
        types.InlineKeyboardButton('📞 Contact', url=f'https://t.me/{YOUR_USERNAME.replace("@","")}'),
    )
    if user_id in admin_ids:
        mk.add(
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast'),
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscriptions'),
            types.InlineKeyboardButton('🟢 Run All', callback_data='run_all'),
            types.InlineKeyboardButton('🔒 Lock Bot', callback_data='lock_bot'),
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'),
            types.InlineKeyboardButton('📋 Pending', callback_data='list_pending'),
        )
    return mk

def create_control_buttons(owner_id, file_name, is_running):
    mk = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        mk.add(
            types.InlineKeyboardButton('🛑 Stop', callback_data=f'stop_{owner_id}_{file_name}'),
            types.InlineKeyboardButton('🔄 Restart', callback_data=f'restart_{owner_id}_{file_name}'),
        )
    else:
        mk.add(types.InlineKeyboardButton('▶️ Start', callback_data=f'start_{owner_id}_{file_name}'))
    mk.add(
        types.InlineKeyboardButton('📋 Logs', callback_data=f'logs_{owner_id}_{file_name}'),
        types.InlineKeyboardButton('🗑 Delete', callback_data=f'delete_{owner_id}_{file_name}'),
    )
    mk.add(types.InlineKeyboardButton('◀️ Back', callback_data='check_files'))
    return mk

def create_subscription_menu():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton('➕ Add Sub', callback_data='sub_add'),
        types.InlineKeyboardButton('➖ Remove Sub', callback_data='sub_remove'),
        types.InlineKeyboardButton('🔍 Check Sub', callback_data='sub_check'),
        types.InlineKeyboardButton('◀️ Back', callback_data='back_main'),
    )
    return mk

def create_admin_panel_menu():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton('➕ Add Admin', callback_data='admin_add'),
        types.InlineKeyboardButton('➖ Remove Admin', callback_data='admin_remove'),
        types.InlineKeyboardButton('📋 List Admins', callback_data='admin_list'),
        types.InlineKeyboardButton('◀️ Back', callback_data='back_main'),
    )
    return mk

def create_all_files_delete_button(owner_id, file_name):
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton(f'🗑 Delete {file_name}', callback_data=f'delete_{owner_id}_{file_name}'))
    return mk

def create_pending_buttons(pending_id):
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton('✅ Approve', callback_data=f'approve_{pending_id}'),
        types.InlineKeyboardButton('❌ Reject', callback_data=f'reject_{pending_id}'),
    )
    return mk

# ============================================================
#  WELCOME / START
# ============================================================
def _logic_send_welcome(message):
    user_id = message.from_user.id
    add_active_user(user_id)
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(int(file_limit)) if file_limit != float('inf') else "∞"
    level = get_user_level(user_id)
    expiry_info = ""
    if user_id in user_subscriptions:
        exp = user_subscriptions[user_id].get('expiry')
        if exp and exp > datetime.now():
            days_left = (exp - datetime.now()).days
            expiry_info = f"\n📅 Sub expires: {days_left} days"

    text = (
        f"👋 Welcome, *{message.from_user.first_name}*!\n\n"
        f"🆔 Your ID: `{user_id}`\n"
        f"🎖 Level: {level}{expiry_info}\n"
        f"📁 Files: {current_files} / {limit_str}\n"
        f"🤖 Bot Status: {'🔒 Locked' if bot_locked else '🟢 Online'}\n\n"
        f"Use the buttons below to get started."
    )
    bot.send_message(message.chat.id, text, parse_mode='Markdown',
                     reply_markup=create_main_menu_inline(user_id))

# ============================================================
#  CHECK FILES
# ============================================================
def _logic_check_files(message_or_call):
    if isinstance(message_or_call, types.CallbackQuery):
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.message.chat.id
        msg_id  = message_or_call.message.message_id
        send_fn = lambda t, **kw: bot.edit_message_text(t, chat_id, msg_id, **kw)
        bot.answer_callback_query(message_or_call.id)
    else:
        user_id = message_or_call.from_user.id
        chat_id = message_or_call.chat.id
        send_fn = lambda t, **kw: bot.send_message(chat_id, t, **kw)

    is_admin = user_id in admin_ids
    files_map = {}
    if is_admin:
        files_map = dict(user_files)
    else:
        if user_id in user_files:
            files_map = {user_id: user_files[user_id]}

    if not files_map:
        send_fn("📂 No files found. Upload a `.py`, `.js`, or `.zip` file.",
                parse_mode='Markdown', reply_markup=create_main_menu_inline(user_id))
        return

    for uid, files in files_map.items():
        for fn, ft in files:
            running = is_bot_running(uid, fn)
            status  = "🟢 Running" if running else "🔴 Stopped"
            key = f"{uid}_{fn}"
            start_t = bot_scripts[key]['start_time'].strftime('%H:%M:%S') if key in bot_scripts else "—"
            text = (f"📄 `{fn}` ({ft.upper()})\n"
                    f"👤 Owner: `{uid}`\n"
                    f"Status: {status}\n"
                    f"Started: {start_t}")
            try:
                bot.send_message(chat_id, text, parse_mode='Markdown',
                                 reply_markup=create_control_buttons(uid, fn, running))
            except Exception as e:
                logger.error(f"check_files send error: {e}")

# ============================================================
#  SEE ALL FILES
# ============================================================
def _logic_see_all_files(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    # Admins see all users' files; regular users see only their own
    if user_id in admin_ids:
        files_map = dict(user_files)
    else:
        files_map = {user_id: user_files[user_id]} if user_id in user_files else {}

    if not files_map:
        bot.send_message(chat_id, "📂 No files found.", reply_markup=create_main_menu_inline(user_id))
        return

    total = sum(len(v) for v in files_map.values())
    bot.send_message(chat_id, f"📁 *All Files* — {total} total\nTap 🗑 Delete to remove a file.", parse_mode='Markdown')

    for uid, files in files_map.items():
        for fn, ft in files:
            running = is_bot_running(uid, fn)
            status = "🟢 Running" if running else "🔴 Stopped"
            key = f"{uid}_{fn}"
            start_t = bot_scripts[key]['start_time'].strftime('%H:%M:%S') if key in bot_scripts else "—"
            text = (f"📄 `{fn}` ({ft.upper()})\n"
                    f"👤 Owner: `{uid}`\n"
                    f"Status: {status} | Started: {start_t}")
            try:
                bot.send_message(chat_id, text, parse_mode='Markdown',
                                 reply_markup=create_all_files_delete_button(uid, fn))
            except Exception as e:
                logger.error(f"see_all_files send error: {e}")

# ============================================================
#  UPLOAD APPROVAL FLOW
# ============================================================
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc     = message.document

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is locked."); return

    if not doc.file_name:
        bot.reply_to(message, "❌ File has no name."); return

    file_ext = os.path.splitext(doc.file_name)[1].lower()
    if file_ext not in ALLOWED_EXT:
        bot.reply_to(message, f"❌ Only {', '.join(ALLOWED_EXT)} allowed."); return

    if doc.file_size > MAX_FILE_MB * 1024 * 1024:
        bot.reply_to(message, f"❌ File too large (max {MAX_FILE_MB} MB)."); return

    file_limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= file_limit:
        bot.reply_to(message, f"❌ File limit reached ({int(file_limit)})."); return

    wait_msg = bot.reply_to(message, f"⏬ Downloading `{doc.file_name}`...", parse_mode='Markdown')
    try:
        fi = bot.get_file(doc.file_id)
        content = bot.download_file(fi.file_path)
    except Exception as e:
        bot.edit_message_text(f"❌ Download failed: {e}", chat_id, wait_msg.message_id)
        return

    # Admins skip approval
    if user_id in admin_ids:
        user_folder = get_user_folder(user_id)
        file_path   = os.path.join(user_folder, doc.file_name)
        ft = 'py' if file_ext == '.py' else 'js' if file_ext == '.js' else 'zip'

        # Auto-replace: if same filename is already running, stop it first
        script_key = f"{user_id}_{doc.file_name}"
        was_running = is_bot_running(user_id, doc.file_name)
        if was_running:
            bot.edit_message_text(
                f"🔄 `{doc.file_name}` is running — stopping for replacement...",
                chat_id, wait_msg.message_id, parse_mode='Markdown'
            )
            kill_process_tree(bot_scripts[script_key])
            del bot_scripts[script_key]
            time.sleep(1)

        with open(file_path, 'wb') as f: f.write(content)

        _icon = '🐍' if file_ext == '.py' else '🟨' if file_ext == '.js' else '📦'
        _status = 'replaced & restarting' if was_running else 'deployed & starting'
        _file_card = (
            "╔══════════════════════════════╗\n"
            "║       ✅  FILE UPLOADED       ║\n"
            "╚══════════════════════════════╝\n\n"
            f"{_icon} *File:* `{doc.file_name}`\n"
            f"👤 *Owner:* `{user_id}`\n"
            f"📦 *Size:* `{_fmt_bytes(doc.file_size)}`\n"
            f"⚡ *Status:* {_status}\n"
        )
        mk_file = types.InlineKeyboardMarkup(row_width=1)
        mk_file.add(types.InlineKeyboardButton('📂 My Files', callback_data='check_files'))
        bot.edit_message_text(_file_card, chat_id, wait_msg.message_id,
                              parse_mode='Markdown', reply_markup=mk_file)

        if file_ext == '.zip':
            handle_zip_file(content, doc.file_name, message)
        else:
            save_user_file(user_id, doc.file_name, ft)
            fp = os.path.join(user_folder, doc.file_name)
            if ft == 'py':
                threading.Thread(target=run_script, args=(fp, user_id, user_folder, doc.file_name, message)).start()
            else:
                threading.Thread(target=run_js_script, args=(fp, user_id, user_folder, doc.file_name, message)).start()
        return

    # Non-admin → save to pending and notify admins
    pending_id  = f"{user_id}_{int(time.time())}_{doc.file_name}"
    pending_path = os.path.join(PENDING_DIR, pending_id.replace('/', '_'))
    with open(pending_path, 'wb') as f: f.write(content)
    ft = 'py' if file_ext == '.py' else 'js' if file_ext == '.js' else 'zip'
    save_pending_upload(pending_id, user_id, doc.file_name, pending_path, ft)

    bot.edit_message_text(
        f"⏳ `{doc.file_name}` uploaded and *pending admin approval*.\n"
        f"You'll be notified when approved or rejected.",
        chat_id, wait_msg.message_id, parse_mode='Markdown'
    )

    # Notify all admins
    user_info = message.from_user
    notify_text = (
        f"📥 *New Upload Pending Approval*\n\n"
        f"👤 User: {user_info.first_name} (`{user_id}`)\n"
        f"📄 File: `{doc.file_name}` ({ft.upper()})\n"
        f"📦 Size: {_fmt_bytes(doc.file_size)}\n"
        f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for aid in admin_ids:
        try:
            bot.send_message(aid, notify_text, parse_mode='Markdown',
                             reply_markup=create_pending_buttons(pending_id))
        except Exception as e:
            logger.error(f"Failed to notify admin {aid}: {e}")

# ============================================================
#  PENDING APPROVAL CALLBACKS
# ============================================================
def approve_upload(call, pending_id):
    bot.answer_callback_query(call.id)
    info = pending_uploads.get(pending_id)
    if not info:
        bot.edit_message_text("⚠️ Pending upload not found (already handled?).",
                              call.message.chat.id, call.message.message_id)
        return

    user_id   = info['user_id']
    file_name = info['file_name']
    file_path = info['file_path']
    ft        = info['file_type']

    if not os.path.exists(file_path):
        bot.edit_message_text("❌ File missing from server.", call.message.chat.id, call.message.message_id)
        remove_pending_upload(pending_id)
        return

    user_folder = get_user_folder(user_id)
    dst = os.path.join(user_folder, file_name)
    shutil.move(file_path, dst)

    save_user_file(user_id, file_name, ft)
    remove_pending_upload(pending_id)

    bot.edit_message_text(
        f"✅ Approved `{file_name}` for user `{user_id}`.",
        call.message.chat.id, call.message.message_id, parse_mode='Markdown'
    )

    # Notify user and start script
    try:
        bot.send_message(user_id,
            f"✅ Your file `{file_name}` was *approved* by admin and is now running!",
            parse_mode='Markdown')
    except: pass

    # Create a dummy message-like object for runner
    class FakeMsg:
        class chat:
            id = user_id
        def __init__(self): pass
    fake = FakeMsg()
    fake.chat.id = user_id

    if ft == 'zip':
        with open(dst, 'rb') as f: content = f.read()
        class FM2:
            from_user = type('U', (), {'id': user_id, 'first_name': 'User'})()
            chat = type('C', (), {'id': user_id})()
            document = None
        import types as pytypes
        fm2 = FM2()
        handle_zip_file(content, file_name, fm2)
    elif ft == 'py':
        threading.Thread(target=run_script, args=(dst, user_id, user_folder, file_name, fake)).start()
    elif ft == 'js':
        threading.Thread(target=run_js_script, args=(dst, user_id, user_folder, file_name, fake)).start()

def reject_upload(call, pending_id):
    bot.answer_callback_query(call.id)
    info = pending_uploads.get(pending_id)
    if not info:
        bot.edit_message_text("⚠️ Already handled.", call.message.chat.id, call.message.message_id)
        return

    user_id   = info['user_id']
    file_name = info['file_name']
    file_path = info['file_path']

    if os.path.exists(file_path):
        try: os.remove(file_path)
        except: pass

    remove_pending_upload(pending_id)

    bot.edit_message_text(
        f"❌ Rejected `{file_name}` for user `{user_id}`.",
        call.message.chat.id, call.message.message_id, parse_mode='Markdown'
    )
    try:
        bot.send_message(user_id,
            f"❌ Your file `{file_name}` was *rejected* by admin.",
            parse_mode='Markdown')
    except: pass

# ============================================================
#  /senddata COMMAND  (admin sends DB/data files to themselves)
# ============================================================
@bot.message_handler(commands=['senddata'])
def cmd_senddata(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return

    args = message.text.split(maxsplit=1)
    # If arg given, send that specific file; otherwise send the whole DB
    if len(args) > 1:
        target = args[1].strip()
        # Prevent path traversal
        target_path = os.path.realpath(os.path.join(DATA_DIR, target))
        if not target_path.startswith(os.path.realpath(DATA_DIR)):
            bot.reply_to(message, "❌ Invalid path."); return
        if not os.path.isfile(target_path):
            bot.reply_to(message, f"❌ File `{target}` not found in data/.", parse_mode='Markdown'); return
        with open(target_path, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"📂 `{target}`", parse_mode='Markdown')
    else:
        # List available data files
        files = os.listdir(DATA_DIR)
        if not files:
            bot.reply_to(message, "📂 data/ is empty."); return
        mk = types.InlineKeyboardMarkup(row_width=1)
        for fn in files:
            if os.path.isfile(os.path.join(DATA_DIR, fn)):
                mk.add(types.InlineKeyboardButton(fn, callback_data=f'dl_data_{fn}'))
        bot.reply_to(message, "📂 Select a file from *data/* to download:", parse_mode='Markdown', reply_markup=mk)

# ============================================================
#  /replacedata COMMAND  (admin uploads a file to replace in data/)
# ============================================================
@bot.message_handler(commands=['replacedata'])
def cmd_replacedata(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    msg = bot.reply_to(message,
        "📤 Send the file you want to place in *data/*.\n"
        "The filename will be preserved.\n/cancel to abort.", parse_mode='Markdown')
    bot.register_next_step_handler(msg, process_replacedata, user_id)

def process_replacedata(message, requesting_admin_id):
    if message.from_user.id != requesting_admin_id: return
    if message.text and message.text.strip().lower() == '/cancel':
        bot.reply_to(message, "Cancelled."); return
    if not message.document:
        bot.reply_to(message, "❌ Please send a file."); return
    doc = message.document
    target_path = os.path.realpath(os.path.join(DATA_DIR, doc.file_name))
    if not target_path.startswith(os.path.realpath(DATA_DIR)):
        bot.reply_to(message, "❌ Invalid filename."); return
    try:
        fi = bot.get_file(doc.file_id)
        content = bot.download_file(fi.file_path)
        with open(target_path, 'wb') as f: f.write(content)
        logger.warning(f"Admin {requesting_admin_id} replaced data/{doc.file_name}")
        bot.reply_to(message,
            f"✅ `{doc.file_name}` saved to *data/*.\n"
            f"🔄 Restarting bot in 3 seconds...",
            parse_mode='Markdown')
        threading.Thread(target=_restart_self, args=(message.chat.id,), daemon=True).start()
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

def _restart_self(notify_chat_id=None):
    """Restart the bot process in-place using os.execv."""
    time.sleep(3)
    try:
        if notify_chat_id:
            try: bot.send_message(notify_chat_id, "🔄 Restarting now...", parse_mode='Markdown')
            except: pass
        cleanup()
        logger.warning("Bot restarting via os.execv...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.critical(f"Restart failed: {e}")
        if notify_chat_id:
            try: bot.send_message(notify_chat_id, f"❌ Restart failed: {e}")
            except: pass

# ============================================================
#  CALLBACK QUERY ROUTER
# ============================================================
@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    cid = call.data
    uid = call.from_user.id

    try:
        if cid.startswith('entrypoint_'):
            bot.answer_callback_query(call.id)
            # format: entrypoint_{user_id}_{filename}
            parts    = cid.split('_', 2)   # ['entrypoint', user_id, filename]
            owner_id = int(parts[1])
            entry    = parts[2]

            # only the owner can pick
            if uid != owner_id:
                bot.answer_callback_query(call.id, "⛔ Not your upload.", show_alert=True)
                return

            ctx_key = None
            if hasattr(bot, '_zip_contexts'):
                ctx_key = next((k for k in bot._zip_contexts if k.startswith(f"{owner_id}_")), None)

            if not ctx_key:
                bot.answer_callback_query(call.id, "⚠️ Session expired. Re-upload the zip.", show_alert=True)
                return

            ctx         = bot._zip_contexts.pop(ctx_key)
            all_bases   = ctx['all_bases']
            user_folder = ctx['user_folder']
            orig_msg    = ctx['message']

            if entry not in all_bases:
                bot.answer_callback_query(call.id, "❌ File not found.", show_alert=True)
                return

            confirm = (
                "╔══════════════════════════════╗\n"
                "║     ▶️  LAUNCHING ENTRY POINT  ║\n"
                "╚══════════════════════════════╝\n\n"
                f"🚀 *Entry point:* `{entry}`\n"
                f"📄 *All files saved:* `{len(all_bases)}`\n\n"
                "Starting now..."
            )
            mk_confirm = types.InlineKeyboardMarkup(row_width=1)
            mk_confirm.add(types.InlineKeyboardButton('📂 My Files', callback_data='check_files'))
            bot.edit_message_text(
                confirm, call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=mk_confirm
            )
            _launch_entry_point(owner_id, user_folder, entry, all_bases, orig_msg)

        elif cid == 'back_main':
            back_to_main(call)
        elif cid == 'check_files':
            _logic_check_files(call)
        elif cid == 'see_all_files':
            _logic_see_all_files(call)
        elif cid == 'upload':
            bot.answer_callback_query(call.id)
            upload_guide = (
                "╔══════════════════════════════╗\n"
                "║        📤  UPLOAD FILE        ║\n"
                "╚══════════════════════════════╝\n\n"
                "Send your file directly to this chat.\n"
                "Supported: `.py`  `.js`  `.zip`\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📦 *ZIP FORMAT GUIDE*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Structure your `.zip` like this:\n\n"
                "```\n"
                "yourbot.zip\n"
                "├── main.py            ← REQUIRED (entry point)\n"
                "├── requirements.txt   ← pip deps (auto-installed)\n"
                "├── Procfile           ← optional, see format below\n"
                "├── config.py          ← optional\n"
                "└── utils/\n"
                "    └── helper.py      ← optional\n"
                "```\n\n"
                "📄 *Procfile format* (inside your zip):\n"
                "```\n"
                "worker: python main.py\n"
                "```\n\n"
                "📋 *requirements.txt format:*\n"
                "```\n"
                "pyTelegramBotAPI\n"
                "requests\n"
                "python-dotenv\n"
                "```\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ *Rules*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "• Entry point must be named `main.py`\n"
                "• At least one `.py` or `.js` required\n"
                "• `requirements.txt` → auto-pip on deploy\n"
                "• Max size: `20 MB`\n"
                "• Nested folders fine — auto-flattened\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🟡 *Non-admin* → pending admin approval\n"
                "🟢 *Admin* → instant deploy + auto-start\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "📎 Drop your file here now."
            )
            mk_upload = types.InlineKeyboardMarkup(row_width=1)
            mk_upload.add(types.InlineKeyboardButton('◀️ Back', callback_data='back_main'))
            bot.send_message(
                call.message.chat.id,
                upload_guide,
                parse_mode='Markdown',
                reply_markup=mk_upload
            )
        elif cid == 'stats':
            bot.answer_callback_query(call.id)
            _logic_statistics(call.message)
        elif cid == 'sysinfo':
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, format_system_stats(), parse_mode='Markdown')
        elif cid == 'speed':
            speed_callback(call)
        elif cid == 'uptime':
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id,
                f"⏱ Bot Uptime: `{get_uptime()}`\n{format_system_stats()}",
                parse_mode='Markdown')
        elif cid == 'broadcast':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "📢 Send message to broadcast:\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_broadcast_message)
        elif cid == 'subscriptions':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            bot.edit_message_text("💳 Subscription Management:", call.message.chat.id,
                                  call.message.message_id, reply_markup=create_subscription_menu())
        elif cid == 'run_all':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id, "Starting all scripts...")
            threading.Thread(target=_logic_run_all_scripts, args=(call,)).start()
        elif cid == 'lock_bot':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            global bot_locked
            bot_locked = not bot_locked
            status = "🔒 Locked" if bot_locked else "🟢 Unlocked"
            bot.answer_callback_query(call.id, f"Bot {status}")
            try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                               reply_markup=create_main_menu_inline(uid))
            except: pass
        elif cid == 'admin_panel':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            bot.edit_message_text("👑 Admin Panel:", call.message.chat.id,
                                  call.message.message_id, reply_markup=create_admin_panel_menu())
        elif cid == 'list_pending':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            _show_pending_list(call)
        # Pending approve/reject
        elif cid.startswith('approve_'):
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            approve_upload(call, cid[len('approve_'):])
        elif cid.startswith('reject_'):
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            reject_upload(call, cid[len('reject_'):])
        # Download data file
        elif cid.startswith('dl_data_'):
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            fn = cid[len('dl_data_'):]
            fp = os.path.realpath(os.path.join(DATA_DIR, fn))
            if not fp.startswith(os.path.realpath(DATA_DIR)) or not os.path.isfile(fp):
                bot.answer_callback_query(call.id, "File not found.", show_alert=True); return
            bot.answer_callback_query(call.id)
            with open(fp, 'rb') as f:
                bot.send_document(call.message.chat.id, f, caption=f"📂 `{fn}`", parse_mode='Markdown')
        # Script controls
        elif cid.startswith('start_'):
            _parse_and_run(call, cid[len('start_'):])
        elif cid.startswith('stop_'):
            _parse_and_stop(call, cid[len('stop_'):])
        elif cid.startswith('restart_'):
            _parse_and_restart(call, cid[len('restart_'):])
        elif cid.startswith('delete_'):
            _parse_and_delete(call, cid[len('delete_'):])
        elif cid.startswith('logs_'):
            _parse_and_logs(call, cid[len('logs_'):])
        # Sub management
        elif cid == 'sub_add':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter `USER_ID DAYS` (e.g. `123456 30`):\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_add_sub)
        elif cid == 'sub_remove':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter USER_ID to remove sub:\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_remove_sub)
        elif cid == 'sub_check':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter USER_ID to check sub:\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_check_sub)
        # Admin mgmt
        elif cid == 'admin_add':
            if uid != OWNER_ID: bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter USER_ID to promote to Admin:\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_add_admin)
        elif cid == 'admin_remove':
            if uid != OWNER_ID: bot.answer_callback_query(call.id, "Owner only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Enter Admin USER_ID to remove:\n/cancel to abort.")
            bot.register_next_step_handler(msg, process_remove_admin)
        elif cid == 'admin_list':
            if uid not in admin_ids: bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
            bot.answer_callback_query(call.id)
            lines = [f"• `{a}` {'(Owner)' if a==OWNER_ID else ''}" for a in sorted(admin_ids)]
            bot.send_message(call.message.chat.id, "👑 *Admin List:*\n" + "\n".join(lines), parse_mode='Markdown')
        elif cid == 'confirm_broadcast':
            _execute_broadcast_confirm(call)
        elif cid == 'cancel_broadcast':
            bot.answer_callback_query(call.id, "Broadcast cancelled.")
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except: pass
        else:
            bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Callback error [{cid}]: {e}", exc_info=True)
        try: bot.answer_callback_query(call.id, "⚠️ Error.", show_alert=True)
        except: pass

def _show_pending_list(call):
    if not pending_uploads:
        bot.send_message(call.message.chat.id, "✅ No pending uploads.")
        return
    for pid, info in list(pending_uploads.items()):
        text = (f"⏳ *Pending Upload*\n"
                f"ID: `{pid[:30]}...`\n"
                f"User: `{info['user_id']}`\n"
                f"File: `{info['file_name']}`\n"
                f"Time: {info['timestamp']}")
        bot.send_message(call.message.chat.id, text, parse_mode='Markdown',
                         reply_markup=create_pending_buttons(pid))

# ============================================================
#  SCRIPT CONTROL CALLBACKS
# ============================================================
def _split_owner_file(data):
    parts = data.split('_', 1)
    return int(parts[0]), parts[1]

def _parse_and_run(call, data):
    owner_id, file_name = _split_owner_file(data)
    uid = call.from_user.id
    if not (uid == owner_id or uid in admin_ids):
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
    files = user_files.get(owner_id, [])
    fi = next((f for f in files if f[0] == file_name), None)
    if not fi:
        bot.answer_callback_query(call.id, "File not found.", show_alert=True); return
    ft = fi[1]; user_folder = get_user_folder(owner_id)
    fp = os.path.join(user_folder, file_name)
    bot.answer_callback_query(call.id, f"Starting {file_name}...")
    if ft == 'py':
        threading.Thread(target=run_script, args=(fp, owner_id, user_folder, file_name, call.message)).start()
    elif ft == 'js':
        threading.Thread(target=run_js_script, args=(fp, owner_id, user_folder, file_name, call.message)).start()

def _parse_and_stop(call, data):
    owner_id, file_name = _split_owner_file(data)
    uid = call.from_user.id
    if not (uid == owner_id or uid in admin_ids):
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
    key = f"{owner_id}_{file_name}"
    if key in bot_scripts:
        kill_process_tree(bot_scripts[key])
        del bot_scripts[key]
        bot.answer_callback_query(call.id, f"Stopped {file_name}.")
    else:
        bot.answer_callback_query(call.id, "Not running.", show_alert=True)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_control_buttons(owner_id, file_name, False))
    except: pass

def _parse_and_restart(call, data):
    owner_id, file_name = _split_owner_file(data)
    uid = call.from_user.id
    if not (uid == owner_id or uid in admin_ids):
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
    key = f"{owner_id}_{file_name}"
    if key in bot_scripts: kill_process_tree(bot_scripts[key]); del bot_scripts[key]
    time.sleep(1)
    files = user_files.get(owner_id, [])
    fi = next((f for f in files if f[0] == file_name), None)
    if not fi: bot.answer_callback_query(call.id, "File not found.", show_alert=True); return
    ft = fi[1]; user_folder = get_user_folder(owner_id)
    fp = os.path.join(user_folder, file_name)
    bot.answer_callback_query(call.id, f"Restarting {file_name}...")
    if ft == 'py':
        threading.Thread(target=run_script, args=(fp, owner_id, user_folder, file_name, call.message)).start()
    elif ft == 'js':
        threading.Thread(target=run_js_script, args=(fp, owner_id, user_folder, file_name, call.message)).start()

def _parse_and_delete(call, data):
    owner_id, file_name = _split_owner_file(data)
    uid = call.from_user.id
    if not (uid == owner_id or uid in admin_ids):
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
    key = f"{owner_id}_{file_name}"
    if key in bot_scripts: kill_process_tree(bot_scripts[key]); del bot_scripts[key]
    user_folder = get_user_folder(owner_id)
    for fn in [file_name, f"{os.path.splitext(file_name)[0]}.log"]:
        fp = os.path.join(user_folder, fn)
        if os.path.exists(fp):
            try: os.remove(fp)
            except: pass
    remove_user_file_db(owner_id, file_name)
    bot.answer_callback_query(call.id, f"Deleted {file_name}.")
    try:
        bot.edit_message_text(f"🗑 `{file_name}` deleted.", call.message.chat.id,
                              call.message.message_id, parse_mode='Markdown')
    except: pass

def _parse_and_logs(call, data):
    owner_id, file_name = _split_owner_file(data)
    uid = call.from_user.id
    if not (uid == owner_id or uid in admin_ids):
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
    user_folder = get_user_folder(owner_id)
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    if not os.path.exists(log_path):
        bot.answer_callback_query(call.id, "No logs yet.", show_alert=True); return
    bot.answer_callback_query(call.id)
    MAX_LOG = 3800
    try:
        size = os.path.getsize(log_path)
        if size == 0: content = "(Log is empty)"
        elif size > 100*1024:
            with open(log_path, 'rb') as f: f.seek(-100*1024, os.SEEK_END); content = f.read().decode('utf-8', errors='ignore')
            content = f"(Last 100KB)\n...\n{content}"
        else:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f: content = f.read()
        if len(content) > MAX_LOG: content = "...\n" + content[-MAX_LOG:]
        bot.send_message(call.message.chat.id,
                         f"📋 Logs for `{file_name}`:\n```\n{content}\n```",
                         parse_mode='Markdown')
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error reading logs: {e}")

# ============================================================
#  BACK TO MAIN
# ============================================================
def back_to_main(call):
    uid = call.from_user.id
    add_active_user(uid)
    file_limit = get_user_file_limit(uid)
    current_files = get_user_file_count(uid)
    limit_str = str(int(file_limit)) if file_limit != float('inf') else "∞"
    level = get_user_level(uid)
    text = (f"👋 *{call.from_user.first_name}*\n\n"
            f"🆔 `{uid}` | 🎖 {level}\n"
            f"📁 Files: {current_files}/{limit_str}\n"
            f"🤖 Bot: {'🔒 Locked' if bot_locked else '🟢 Online'}")
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=create_main_menu_inline(uid))
    except: pass

# ============================================================
#  SPEED CALLBACK
# ============================================================
def speed_callback(call):
    uid = call.from_user.id
    t0 = time.time()
    bot.answer_callback_query(call.id)
    latency = round((time.time() - t0) * 1000, 2)
    s = get_system_stats()
    msg = (f"⚡ *Speed & Status*\n\n"
           f"📡 API Latency: `{latency} ms`\n"
           f"🖥 CPU: `{s['cpu']}%`\n"
           f"🧠 RAM: `{s['ram_pct']}%` ({s['ram_used']}/{s['ram_total']})\n"
           f"💾 Disk: `{s['disk_pct']}%`\n"
           f"🎖 Level: {get_user_level(uid)}\n"
           f"🤖 Status: {'🔒 Locked' if bot_locked else '🟢 Online'}")
    try:
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown', reply_markup=create_main_menu_inline(uid))
    except: pass

# ============================================================
#  STATISTICS
# ============================================================
def _logic_statistics(message):
    uid = message.from_user.id if hasattr(message, 'from_user') else 0
    running = sum(1 for k, v in list(bot_scripts.items()) if is_bot_running(
        int(k.split('_', 1)[0]), v['file_name']))
    total_files = sum(len(v) for v in user_files.values())
    s = get_system_stats()
    text = (f"📊 *Bot Statistics*\n\n"
            f"👥 Total Users: `{len(active_users)}`\n"
            f"📁 Total Files: `{total_files}`\n"
            f"🟢 Running Scripts: `{running}`\n"
            f"⏳ Pending Approvals: `{len(pending_uploads)}`\n\n"
            f"🖥 CPU: `{s['cpu']}%` | 🧠 RAM: `{s['ram_pct']}%`\n"
            f"💾 Disk: `{s['disk_pct']}%`\n"
            f"⏱ Bot Uptime: `{get_uptime()}`")
    if uid in admin_ids:
        text += (f"\n\n🛡 *Admin Info*\n"
                 f"Admins: `{len(admin_ids)}`\n"
                 f"Subscribed: `{len(user_subscriptions)}`\n"
                 f"Bot Lock: {'🔒 Locked' if bot_locked else '🟢 Unlocked'}")
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

# ============================================================
#  SUBSCRIPTION MANAGEMENT
# ============================================================
def process_add_sub(message):
    uid = message.from_user.id
    if uid not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        parts = message.text.split()
        if len(parts) != 2: raise ValueError("Format: USER_ID DAYS")
        target_id, days = int(parts[0]), int(parts[1])
        if target_id <= 0 or days <= 0: raise ValueError("Must be positive")
        curr = user_subscriptions.get(target_id, {}).get('expiry')
        start = max(datetime.now(), curr) if curr else datetime.now()
        new_exp = start + timedelta(days=days)
        save_subscription(target_id, new_exp)
        bot.reply_to(message, f"✅ Sub for `{target_id}` extended by `{days}` days.\nExpires: `{new_exp.strftime('%Y-%m-%d')}`",
                     parse_mode='Markdown')
        try: bot.send_message(target_id, f"✅ Sub activated! Expires: `{new_exp.strftime('%Y-%m-%d')}`", parse_mode='Markdown')
        except: pass
    except ValueError as e:
        bot.reply_to(message, f"❌ {e}. Format: `USER_ID DAYS`", parse_mode='Markdown')

def process_remove_sub(message):
    uid = message.from_user.id
    if uid not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        target_id = int(message.text.strip())
        remove_subscription_db(target_id)
        bot.reply_to(message, f"✅ Sub for `{target_id}` removed.", parse_mode='Markdown')
        try: bot.send_message(target_id, "❌ Your subscription has been removed.")
        except: pass
    except ValueError:
        bot.reply_to(message, "❌ Invalid USER_ID.")

def process_check_sub(message):
    uid = message.from_user.id
    if uid not in admin_ids: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        target_id = int(message.text.strip())
        if target_id in user_subscriptions:
            exp = user_subscriptions[target_id]['expiry']
            if exp > datetime.now():
                days = (exp - datetime.now()).days
                bot.reply_to(message, f"✅ `{target_id}` — Active sub.\nExpires: `{exp.strftime('%Y-%m-%d')}` ({days} days left)",
                             parse_mode='Markdown')
            else:
                bot.reply_to(message, f"⚠️ `{target_id}` — Sub expired on `{exp.strftime('%Y-%m-%d')}`",
                             parse_mode='Markdown')
        else:
            bot.reply_to(message, f"❌ `{target_id}` — No subscription.", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "❌ Invalid USER_ID.")

# ============================================================
#  ADMIN MANAGEMENT
# ============================================================
def process_add_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        new_id = int(message.text.strip())
        if new_id in admin_ids:
            bot.reply_to(message, f"`{new_id}` is already an admin.", parse_mode='Markdown'); return
        add_admin_db(new_id)
        bot.reply_to(message, f"✅ `{new_id}` promoted to Admin.", parse_mode='Markdown')
        try: bot.send_message(new_id, "🎉 You are now an Admin!")
        except: pass
    except ValueError:
        bot.reply_to(message, "❌ Invalid USER_ID.")

def process_remove_admin(message):
    if message.from_user.id != OWNER_ID: return
    if message.text.lower() == '/cancel': bot.reply_to(message, "Cancelled."); return
    try:
        rem_id = int(message.text.strip())
        if rem_id == OWNER_ID:
            bot.reply_to(message, "❌ Cannot remove Owner."); return
        if remove_admin_db(rem_id):
            bot.reply_to(message, f"✅ Admin `{rem_id}` removed.", parse_mode='Markdown')
            try: bot.send_message(rem_id, "You are no longer an Admin.")
            except: pass
        else:
            bot.reply_to(message, f"❌ `{rem_id}` not found in admins.", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "❌ Invalid USER_ID.")

# ============================================================
#  BROADCAST
# ============================================================
def process_broadcast_message(message):
    uid = message.from_user.id
    if uid not in admin_ids: return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Broadcast cancelled."); return
    has_content = message.text or message.photo or message.video or message.document
    if not has_content:
        msg = bot.reply_to(message, "❌ Empty message. Send text/media or /cancel.")
        bot.register_next_step_handler(msg, process_broadcast_message)
        return
    preview = message.text[:800] if message.text else "(Media)"
    mk = types.InlineKeyboardMarkup()
    mk.row(
        types.InlineKeyboardButton("✅ Confirm", callback_data='confirm_broadcast'),
        types.InlineKeyboardButton("❌ Cancel", callback_data='cancel_broadcast')
    )
    bot.reply_to(message, f"📢 Broadcast preview:\n```\n{preview}\n```\nSend to *{len(active_users)}* users?",
                 parse_mode='Markdown', reply_markup=mk)

def _execute_broadcast_confirm(call):
    uid = call.from_user.id
    if uid not in admin_ids:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True); return
    orig = call.message.reply_to_message
    if not orig:
        bot.answer_callback_query(call.id, "Cannot find original message.", show_alert=True); return
    bot.answer_callback_query(call.id, "Broadcasting...")
    bot.edit_message_text(f"📢 Broadcasting to {len(active_users)} users...",
                          call.message.chat.id, call.message.message_id, reply_markup=None)
    threading.Thread(target=_do_broadcast, args=(orig, call.message.chat.id)).start()

def _do_broadcast(orig, admin_chat_id):
    sent = failed = blocked = 0
    users = list(active_users)
    for i, tuid in enumerate(users):
        try:
            if orig.text: bot.send_message(tuid, orig.text)
            elif orig.photo: bot.send_photo(tuid, orig.photo[-1].file_id, caption=orig.caption)
            elif orig.video: bot.send_video(tuid, orig.video.file_id, caption=orig.caption)
            sent += 1
        except telebot.apihelper.ApiTelegramException as e:
            desc = str(e).lower()
            if any(s in desc for s in ['blocked', 'deactivated', 'not found', 'kicked']):
                blocked += 1
            elif 'flood' in desc or 'too many' in desc:
                m = re.search(r'retry after (\d+)', desc)
                wait = int(m.group(1)) + 1 if m else 5
                time.sleep(wait)
                try:
                    if orig.text: bot.send_message(tuid, orig.text)
                    sent += 1
                except: failed += 1
            else: failed += 1
        except: failed += 1
        if (i+1) % 25 == 0: time.sleep(1.5)
        elif i % 5 == 0: time.sleep(0.2)
    msg = (f"📢 *Broadcast Done*\n\n✅ Sent: {sent}\n❌ Failed: {failed}\n"
           f"🚫 Blocked: {blocked}\n👥 Total: {len(users)}")
    try: bot.send_message(admin_chat_id, msg, parse_mode='Markdown')
    except: pass

# ============================================================
#  RUN ALL SCRIPTS
# ============================================================
def _logic_run_all_scripts(call_or_msg):
    if isinstance(call_or_msg, types.CallbackQuery):
        uid = call_or_msg.from_user.id
        chat_id = call_or_msg.message.chat.id
        msg_obj = call_or_msg.message
    else:
        uid = call_or_msg.from_user.id
        chat_id = call_or_msg.chat.id
        msg_obj = call_or_msg
    if uid not in admin_ids:
        bot.send_message(chat_id, "❌ Admin only."); return
    started = skipped = 0
    for owner_id, files in dict(user_files).items():
        user_folder = get_user_folder(owner_id)
        for fn, ft in files:
            if not is_bot_running(owner_id, fn):
                fp = os.path.join(user_folder, fn)
                if os.path.exists(fp):
                    if ft == 'py':
                        threading.Thread(target=run_script, args=(fp, owner_id, user_folder, fn, msg_obj)).start()
                    elif ft == 'js':
                        threading.Thread(target=run_js_script, args=(fp, owner_id, user_folder, fn, msg_obj)).start()
                    started += 1; time.sleep(0.5)
                else: skipped += 1
    bot.send_message(chat_id, f"🟢 Run All: started `{started}`, skipped `{skipped}`.", parse_mode='Markdown')

# ============================================================
#  COMMAND HANDLERS
# ============================================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    add_active_user(message.from_user.id)
    if bot_locked and message.from_user.id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is currently locked."); return
    _logic_send_welcome(message)

@bot.message_handler(commands=['ping'])
def cmd_ping(message):
    t0 = time.time()
    m = bot.reply_to(message, "🏓 Pong!")
    lat = round((time.time()-t0)*1000, 2)
    bot.edit_message_text(f"🏓 Pong!\n📡 Latency: `{lat} ms`\n⏱ Uptime: `{get_uptime()}`",
                          message.chat.id, m.message_id, parse_mode='Markdown')

@bot.message_handler(commands=['status'])
def cmd_status(message):
    _logic_statistics(message)

@bot.message_handler(commands=['sysinfo', 'system'])
def cmd_sysinfo(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    bot.reply_to(message, format_system_stats(), parse_mode='Markdown')

@bot.message_handler(commands=['uptime'])
def cmd_uptime(message):
    bot.reply_to(message, f"⏱ Bot Uptime: `{get_uptime()}`", parse_mode='Markdown')

@bot.message_handler(commands=['checkfiles'])
def cmd_checkfiles(message):
    _logic_check_files(message)

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    msg = bot.reply_to(message, "📢 Send broadcast message:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)

@bot.message_handler(commands=['lock', 'unlock'])
def cmd_lock_unlock(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    global bot_locked
    if message.text.startswith('/lock'): bot_locked = True; bot.reply_to(message, "🔒 Bot locked.")
    else: bot_locked = False; bot.reply_to(message, "🟢 Bot unlocked.")

@bot.message_handler(commands=['addadmin'])
def cmd_addadmin(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Owner only."); return
    msg = bot.reply_to(message, "Enter USER_ID to promote:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_admin)

@bot.message_handler(commands=['removeadmin'])
def cmd_removeadmin(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "❌ Owner only."); return
    msg = bot.reply_to(message, "Enter Admin USER_ID to remove:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_admin)

@bot.message_handler(commands=['addsub'])
def cmd_addsub(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    msg = bot.reply_to(message, "Enter `USER_ID DAYS`:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_sub)

@bot.message_handler(commands=['removesub'])
def cmd_removesub(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    msg = bot.reply_to(message, "Enter USER_ID to remove sub:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_sub)

@bot.message_handler(commands=['checksub'])
def cmd_checksub(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    msg = bot.reply_to(message, "Enter USER_ID to check:\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_check_sub)

@bot.message_handler(commands=['pending'])
def cmd_pending(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    if not pending_uploads:
        bot.reply_to(message, "✅ No pending uploads."); return
    for pid, info in list(pending_uploads.items()):
        text = (f"⏳ *Pending*\n"
                f"User: `{info['user_id']}`\n"
                f"File: `{info['file_name']}`\n"
                f"Time: {info['timestamp']}")
        bot.send_message(message.chat.id, text, parse_mode='Markdown',
                         reply_markup=create_pending_buttons(pid))

@bot.message_handler(commands=['restart'])
def cmd_restart(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    bot.reply_to(message, "🔄 Restarting bot in 3 seconds...")
    threading.Thread(target=_restart_self, args=(message.chat.id,), daemon=True).start()

@bot.message_handler(commands=['runall'])
def cmd_runall(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "❌ Admin only."); return
    _logic_run_all_scripts(message)

# ============================================================
#  FILE UPLOAD HANDLER
# ============================================================
@bot.message_handler(content_types=['document'])
def handle_document(message):
    if bot_locked and message.from_user.id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is locked."); return
    add_active_user(message.from_user.id)
    handle_file_upload_doc(message)

# ============================================================
#  CLEANUP
# ============================================================
def cleanup():
    logger.warning("Shutting down. Stopping all scripts...")
    for key, info in list(bot_scripts.items()):
        logger.info(f"Stopping {key}")
        kill_process_tree(info)
    logger.warning("Cleanup done.")

atexit.register(cleanup)

# ============================================================
#  ENTRYPOINT
# ============================================================
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🤖 Hosting Bot Starting")
    logger.info(f"Python: {sys.version.split()[0]}")
    logger.info(f"Owner ID: {OWNER_ID}")
    logger.info(f"Admins: {admin_ids}")
    logger.info(f"Data dir: {DATA_DIR}")
    logger.info(f"Upload dir: {UPLOAD_BOTS_DIR}")
    logger.info("=" * 50)
    keep_alive()
    logger.info("Starting bot polling...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, logger_level=logging.WARNING)
        except requests.exceptions.ReadTimeout:
            logger.warning("Polling ReadTimeout. Retrying in 5s..."); time.sleep(5)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}. Retrying in 15s..."); time.sleep(15)
        except Exception as e:
            logger.critical(f"Polling crashed: {e}", exc_info=True); time.sleep(30)
        finally:
            time.sleep(1)
