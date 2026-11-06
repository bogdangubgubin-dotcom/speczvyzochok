import os
import sqlite3
import hashlib
import time
import json
import random
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from pywebpush import webpush, WebPushException

# 1. Настройка Flask с папкой 'templates'
app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static'
)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'speczvyazok_secret_key_2026')

# 2. Инициализация SocketIO
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    max_http_buffer_size=25 * 1024 * 1024, 
    async_mode=None, 
    ping_timeout=10, 
    ping_interval=5
)

# Переменные окружения и ключи
DATABASE_URL = os.environ.get('DATABASE_URL')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@example.com')

# --- ПОДКЛЮЧЕНИЕ И ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---

def get_db():
    if DATABASE_URL:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode='require')
        return conn, 'postgres'
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        DB_PATH = os.path.join(BASE_DIR, "messenger.db")
        conn = sqlite3.connect(DB_PATH, timeout=10)
        return conn, 'sqlite'

def init_db():
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'postgres':
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role VARCHAR(50) DEFAULT 'user',
                avatar TEXT DEFAULT '',
                last_seen BIGINT DEFAULT 0
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) UNIQUE NOT NULL,
                creator VARCHAR(255) NOT NULL,
                mods TEXT DEFAULT '',
                banned TEXT DEFAULT '',
                pinned_msg TEXT DEFAULT ''
            );
        """)
        cur.execute("INSERT INTO channels (name, creator) VALUES ('#general', 'system') ON CONFLICT (name) DO NOTHING;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                sender VARCHAR(255) NOT NULL,
                target VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                msg_type VARCHAR(50) DEFAULT 'text',
                reply_to INT DEFAULT NULL,
                reply_text TEXT DEFAULT '',
                reactions TEXT DEFAULT '{}',
                ttl INT DEFAULT 0,
                is_read INT DEFAULT 0,
                is_edited INT DEFAULT 0,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                subscription TEXT UNIQUE NOT NULL
            );
        """)
        cur.execute("UPDATE users SET role = 'admin' WHERE username = 'totr';")
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                avatar TEXT DEFAULT '',
                last_seen INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                creator TEXT NOT NULL,
                mods TEXT DEFAULT '',
                banned TEXT DEFAULT '',
                pinned_msg TEXT DEFAULT ''
            )
        """)
        cur.execute("INSERT OR IGNORE INTO channels (name, creator) VALUES ('#general', 'system')")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                target TEXT NOT NULL,
                message TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                reply_to INTEGER DEFAULT NULL,
                reply_text TEXT DEFAULT '',
                reactions TEXT DEFAULT '{}',
                ttl INTEGER DEFAULT 0,
                is_read INTEGER DEFAULT 0,
                is_edited INTEGER DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                subscription TEXT UNIQUE NOT NULL
            )
        """)
        cur.execute("UPDATE users SET role = 'admin' WHERE username = 'totr'")

    conn.commit()
    conn.close()

init_db()

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

# --- ЛОГИКА ИГРЫ "МАФИЯ" ---

class MafiaRoom:
    def __init__(self, room_id, name):
        self.room_id = room_id
        self.name = name
        self.state = 'lobby'
        self.players = {}
        self.max_slots = 6
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        self.day_votes = {}
        self.logs = []
        self.timer_end = 0

    def add_player(self, username):
        if self.state == 'lobby' and username not in self.players and len(self.players) < self.max_slots:
            taken_slots = [p['slot'] for p in self.players.values()]
            free_slot = 0
            for s in range(self.max_slots):
                if s not in taken_slots:
                    free_slot = s
                    break
            self.players[username] = {'role': None, 'alive': True, 'mic': True, 'slot': free_slot}
            return True
        return False

    def remove_player(self, username):
        if username in self.players:
            del self.players[username]
            if len(self.players) == 0:
                self.state = 'lobby'
                self.logs = []
                self.timer_end = 0
            return True
        return False

    def start_game(self):
        plist = list(self.players.keys())
        if len(plist) < 3: return False, "Нужно минимум 3 игрока!"
        random.shuffle(plist)
        mafia_count = max(1, len(plist) // 3)
        roles = ['mafia'] * mafia_count
        if len(plist) >= 4: roles.append('doc')
        if len(plist) >= 5: roles.append('cop')
        while len(roles) < len(plist): roles.append('citizen')
        random.shuffle(roles)

        for i, u in enumerate(plist):
            self.players[u]['role'] = roles[i]
            self.players[u]['alive'] = True
            self.players[u]['mic'] = False

        self.state = 'night'
        self.timer_end = int(time.time()) + 60
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        self.day_votes = {}
        self.logs = ["Игра началась! Наступила ночь (60 сек)."]
        return True, "Игра запущена!"

    def check_win(self):
        alive_mafia = sum(1 for p in self.players.values() if p['alive'] and p['role'] == 'mafia')
        alive_others = sum(1 for p in self.players.values() if p['alive'] and p['role'] != 'mafia')
        if alive_mafia == 0:
            self.state = 'ended'
            self.timer_end = 0
            return "Мирные жители победили!"
        if alive_mafia >= alive_others:
            self.state = 'ended'
            self.timer_end = 0
            return "Мафия победила!"
        return None

    def process_night(self):
        mafia_targets = list(self.night_actions['mafia'].values())
        killed = max(set(mafia_targets), key=mafia_targets.count) if mafia_targets else None
        saved = self.night_actions['doc']
        msg = "Утро. "
        if killed and killed != saved:
            self.players[killed]['alive'] = False
            self.players[killed]['mic'] = False
            msg += f"Ночью был убит игрок **{killed}**."
        else: msg += "Ночью никто не погиб!"
        self.logs.append(msg)
        win = self.check_win()
        if win: self.logs.append(f"🎉 **{win}**")
        else:
            self.state = 'day'
            self.timer_end = int(time.time()) + 60
            for u, p in self.players.items():
                if p['alive']: p['mic'] = True
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        return msg

    def process_day(self):
        if not self.day_votes: self.logs.append("Днем никто не был исключен.")
        else:
            targets = list(self.day_votes.values())
            exiled = max(set(targets), key=targets.count)
            self.players[exiled]['alive'] = False
            self.players[exiled]['mic'] = False
            self.logs.append(f"Город выгнал игрока **{exiled}** ({self.players[exiled]['role']}).")
        win = self.check_win()
        if win: self.logs.append(f"🎉 **{win}**")
        else:
            self.state = 'night'
            self.timer_end = int(time.time()) + 60
            self.logs.append("Наступает ночь... Город засыпает (60 сек).")
            for u, p in self.players.items(): p['mic'] = False
        self.day_votes = {}

# --- ГЛОБАЛЬНЫЕ СТРУКТУРЫ ДАННЫХ В ПАМЯТИ ---
mafia_rooms = {f"room_{i}": MafiaRoom(f"room_{i}", f"Комната №{i}") for i in range(1, 6)}
call_rooms = {}         
online_users = {}       
user_active_chats = {} 

# --- ФУНКЦИЯ WEB PUSH ---

def send_web_push(username, title, body):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("SELECT subscription FROM push_subscriptions WHERE username = %s", (username,))
    else:
        cur.execute("SELECT subscription FROM push_subscriptions WHERE username = ?", (username,))
    rows = cur.fetchall()
    conn.close()

    payload = json.dumps({'title': title, 'body': body})

    for row in rows:
        try:
            sub_info = json.loads(row[0])
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                ttl=86400,
                headers={"Urgency": "high"}
            )
        except WebPushException as ex:
            print("WebPush Error:", ex)
        except Exception as e:
            print("Push Exception:", e)

# --- HTTP МАРШРУТЫ (ROUTES) ---

@app.route('/')
def index(): 
    return render_template('index.html', vapid_public_key=VAPID_PUBLIC_KEY)

@app.route('/manifest.json')
def serve_manifest():
    return {
        "name": "Спецзвязочок Ultra",
        "short_name": "Спецзвязок",
        "description": "Защищенный мессенджер с поддержкой групповых звонков и игр",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#121212",
        "theme_color": "#1F6AA5",
        "icons": [
            {
                "src": "https://cdn-icons-png.flaticon.com/512/732/732200.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    }, 200, {'Content-Type': 'application/json'}

@app.route('/sw.js')
def serve_sw():
    sw_code = """
    self.addEventListener('install', (e) => { self.skipWaiting(); });
    self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
    self.addEventListener('fetch', (e) => {});

    self.addEventListener('push', function(event) {
        let data = {};
        if (event.data) {
            try { data = event.data.json(); } 
            catch(e) { data = { title: 'Новое сообщение', body: event.data.text() }; }
        }
        const title = data.title || 'Спецзвязочок';
        const options = {
            body: data.body || '',
            icon: 'https://cdn-icons-png.flaticon.com/512/732/732200.png',
            vibrate: [200, 100, 200, 100, 200],
            tag: 'speczvyazok-msg',
            renotify: true,
            data: { url: '/' }
        };
        event.waitUntil(self.registration.showNotification(title, options));
    });

    self.addEventListener('notificationclick', function(event) {
        event.notification.close();
        event.waitUntil(
            clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
                for (let client of clientList) {
                    if ('focus' in client) return client.focus();
                }
                if (clients.openWindow) return clients.openWindow('/');
            })
        );
    });
    """
    return sw_code, 200, {'Content-Type': 'application/javascript'}

# --- СОБЫТИЯ SOCKET.IO ---

@socketio.on('connect')
def on_connect(): pass

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    for uname, s_id in list(online_users.items()):
        if s_id == sid:
            del online_users[uname]
            if uname in user_active_chats: del user_active_chats[uname]
            conn, db_type = get_db()
            cur = conn.cursor()
            now = int(time.time())
            if db_type == 'postgres': cur.execute("UPDATE users SET last_seen = %s WHERE username = %s", (now, uname))
            else: cur.execute("UPDATE users SET last_seen = ? WHERE username = ?", (now, uname))
            conn.commit()
            conn.close()
            on_get_users()
            break
            
    for room_id, users in list(call_rooms.items()):
        if sid in users:
            users.remove(sid)
            emit('user_left_call', {'sid': sid}, room=room_id, include_self=False)
            if not users:
                del call_rooms[room_id]

@socketio.on('update_current_chat')
def on_update_current_chat(data):
    user = data.get('user')
    active_chat = data.get('active_chat')
    if user and active_chat:
        user_active_chats[user] = active_chat

@socketio.on('save_push_subscription')
def on_save_push_subscription(data):
    username = data.get('user')
    sub_json = json.dumps(data.get('subscription'))
    if not username or not sub_json: return
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        if db_type == 'postgres':
            cur.execute("""
                INSERT INTO push_subscriptions (username, subscription) 
                VALUES (%s, %s) ON CONFLICT (subscription) DO NOTHING
            """, (username, sub_json))
        else:
            cur.execute("""
                INSERT OR IGNORE INTO push_subscriptions (username, subscription) 
                VALUES (?, ?)
            """, (username, sub_json))
        conn.commit()
    except Exception as e:
        print("Save push sub error:", e)
    finally:
        conn.close()

@socketio.on('login')
def on_login(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'postgres':
        cur.execute("SELECT password_hash, salt, role, avatar FROM users WHERE username = %s", (username,))
    else:
        cur.execute("SELECT password_hash, salt, role, avatar FROM users WHERE username = ?", (username,))
        
    row = cur.fetchone()

    if row:
        pwd_hash, salt, role, avatar = row
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            conn.close()
            online_users[username] = request.sid
            user_active_chats[username] = '#general'
            join_room(username)
            join_room('#general')
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role, 'avatar': avatar})
            on_get_users()
        else:
            conn.close()
            emit('auth_response', {'status': 'error', 'message': 'Неверный пароль'})
    else:
        conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Пользователь не найден'})

@socketio.on('register')
def on_register(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password: return
    pwd_hash, salt = hash_password(password)
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        role = 'admin' if username == 'totr' else 'user'
        if db_type == 'postgres':
            cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (%s, %s, %s, %s)", (username, pwd_hash, salt, role))
        else:
            cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)", (username, pwd_hash, salt, role))
        conn.commit()
        emit('auth_response', {'status': 'error', 'message': 'Успешно! Теперь войдите.'})
    except Exception:
        emit('auth_response', {'status': 'error', 'message': 'Логин занят'})
    finally: conn.close()

# --- WEBRTC ЗВОНКИ ---

@socketio.on('join_call')
def handle_join_call(data):
    room_id = data.get('roomId', 'default-room')
    sid = request.sid
    
    join_room(room_id)
    
    if room_id not in call_rooms:
        call_rooms[room_id] = []
    
    existing_users = call_rooms[room_id].copy()
    
    if sid not in call_rooms[room_id]:
        call_rooms[room_id].append(sid)
        
    emit('current_room_users', {'users': existing_users})
    emit('user_joined_call', {'sid': sid}, room=room_id, include_self=False)

@socketio.on('signal_to_peer')
def handle_signal_to_peer(data):
    target_sid = data.get('targetSid')
    emit('signal_from_peer', {
        'senderSid': request.sid,
        'signal': data.get('signal')
    }, room=target_sid)

@socketio.on('leave_call')
def handle_leave_call(data):
    room_id = data.get('roomId', 'default-room')
    sid = request.sid
    
    leave_room(room_id)
    
    if room_id in call_rooms and sid in call_rooms[room_id]:
        call_rooms[room_id].remove(sid)
        if not call_rooms[room_id]:
            del call_rooms[room_id]
            
    emit('user_left_call', {'sid': sid}, room=room_id, include_self=False)

@socketio.on('call_start')
def on_call_start(data):
    caller = data.get('caller')
    target = data.get('target')
    
    send_web_push(target, f"📞 Входящий звонок от {caller}", "Вам звонят! Откройте приложение, чтобы ответить.")

    if target not in online_users:
        conn, db_type = get_db()
        cur = conn.cursor()
        msg_text = "📞 Пропущенный звонок"
        msg_type = "text"
        
        if db_type == 'postgres':
            cur.execute("""
                INSERT INTO messages (sender, target, message, msg_type) 
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (caller, target, msg_text, msg_type))
            msg_id = cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO messages (sender, target, message, msg_type) 
                VALUES (?, ?, ?, ?)
            """, (caller, target, msg_text, msg_type))
            msg_id = cur.lastrowid
            
        conn.commit()
        conn.close()
        
        missed_msg = {
            'id': msg_id,
            'sender': caller,
            'target': target,
            'msg': msg_text,
            'msg_type': msg_type,
            'is_read': 0,
            'is_edited': 0
        }
        emit('new_msg', missed_msg, room=request.sid)
        emit('call_failed', {'target': target, 'reason': 'offline'}, room=request.sid)
    else:
        call_msg = {
            'id': int(time.time() * 1000),
            'sender': caller,
            'target': target,
            'msg': '📞 Входящий звонок...',
            'msg_type': 'call_card',
            'is_read': 0,
            'is_edited': 0
        }
        emit('new_msg', call_msg, room=target)
        emit('new_msg', call_msg, room=caller)
        emit('incoming_call', data, room=target)

@socketio.on('call_accept')
def on_call_accept(data): emit('call_accepted', data, room=data['target'])

@socketio.on('call_signal')
def on_call_signal(data): emit('call_signal', data, room=data['target'])

@socketio.on('call_end')
def on_call_end(data):
    target = data.get('target')
    caller = data.get('caller')
    if target: emit('call_ended', data, room=target)
    if caller: emit('call_ended', data, room=caller)

# --- КАНАЛЫ И ПОЛЬЗОВАТЕЛИ ---

@socketio.on('create_channel')
def on_create_channel(data):
    ch_name = data['name'].strip()
    creator = data['creator']
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        if db_type == 'postgres':
            cur.execute("INSERT INTO channels (name, creator) VALUES (%s, %s)", (ch_name, creator))
        else:
            cur.execute("INSERT INTO channels (name, creator) VALUES (?, ?)", (ch_name, creator))
        conn.commit()
        join_room(ch_name)
    except Exception: pass
    finally: conn.close()
    emit('channels_list', {'channels': get_all_channels()}, broadcast=True)

def get_all_channels():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM channels")
    chs = [{'name': r[0]} for r in cur.fetchall()]
    conn.close()
    return chs

@socketio.on('get_channels')
def on_get_channels(): emit('channels_list', {'channels': get_all_channels()})

@socketio.on('admin_get_users')
def on_admin_get_users():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users")
    users = [{'username': r[0], 'role': r[1]} for r in cur.fetchall()]
    conn.close()
    emit('admin_users_data', {'users': users})

@socketio.on('admin_change_role')
def on_admin_change_role(data):
    admin = data['admin']
    target_user = data['target_user']
    new_role = data['role']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("SELECT role FROM users WHERE username = %s", (admin,))
        row = cur.fetchone()
        if (row and row[0] == 'admin') or admin == 'totr':
            cur.execute("UPDATE users SET role = %s WHERE username = %s", (new_role, target_user))
            conn.commit()
    else:
        cur.execute("SELECT role FROM users WHERE username = ?", (admin,))
        row = cur.fetchone()
        if (row and row[0] == 'admin') or admin == 'totr':
            cur.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, target_user))
            conn.commit()
    conn.close()
    on_admin_get_users()

@socketio.on('admin_delete_user')
def on_admin_delete_user(data):
    admin = data['admin']
    target_user = data['target_user']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("SELECT role FROM users WHERE username = %s", (admin,))
        row = cur.fetchone()
        if (row and row[0] == 'admin') or admin == 'totr':
            cur.execute("DELETE FROM users WHERE username = %s", (target_user,))
            conn.commit()
    else:
        cur.execute("SELECT role FROM users WHERE username = ?", (admin,))
        row = cur.fetchone()
        if (row and row[0] == 'admin') or admin == 'totr':
            cur.execute("DELETE FROM users WHERE username = ?", (target_user,))
            conn.commit()
    conn.close()
    on_admin_get_users()

@socketio.on('update_avatar')
def on_update_avatar(data):
    user = data['user']
    avatar = data['avatar']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE users SET avatar = %s WHERE username = %s", (avatar, user))
    else:
        cur.execute("UPDATE users SET avatar = ? WHERE username = ?", (avatar, user))
    conn.commit()
    conn.close()
    emit('avatar_updated', {'user': user, 'avatar': avatar}, broadcast=True)

@socketio.on('change_password')
def on_change_password(data):
    user = data['user']
    old_p = data['old_pass']
    new_p = data['new_pass']
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'postgres':
        cur.execute("SELECT password_hash, salt FROM users WHERE username = %s", (user,))
    else:
        cur.execute("SELECT password_hash, salt FROM users WHERE username = ?", (user,))
        
    row = cur.fetchone()

    if row:
        pwd_hash, salt = row
        check_hash, _ = hash_password(old_p, salt)
        if check_hash == pwd_hash:
            new_hash, new_salt = hash_password(new_p)
            if db_type == 'postgres':
                cur.execute("UPDATE users SET password_hash = %s, salt = %s WHERE username = %s", (new_hash, new_salt, user))
            else:
                cur.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (new_hash, new_salt, user))
            conn.commit()
            conn.close()
            emit('pass_response', {'status': 'ok', 'message': 'Пароль успешно изменен!'})
            return
    conn.close()
    emit('pass_response', {'status': 'error', 'message': 'Неверный старый пароль!'})

@socketio.on('get_users')
def on_get_users():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role, last_seen, avatar FROM users")
    users = [{'username': r[0], 'online': (r[0] in online_users), 'last_seen': r[2], 'avatar': r[3]} for r in cur.fetchall()]
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('user_typing')
def on_user_typing(data): emit('display_typing', data, room=data['target'], include_self=False)

@socketio.on('pin_msg')
def on_pin_msg(data):
    target = data['target']
    msg = data['msg']
    if target.startswith('#'):
        conn, db_type = get_db()
        cur = conn.cursor()
        if db_type == 'postgres':
            cur.execute("UPDATE channels SET pinned_msg = %s WHERE name = %s", (msg, target))
        else:
            cur.execute("UPDATE channels SET pinned_msg = ? WHERE name = ?", (msg, target))
        conn.commit()
        conn.close()
    emit('update_pinned', {'target': target, 'msg': msg}, broadcast=True)

@socketio.on('mark_read')
def on_mark_read(data):
    user = data['user']
    target = data['target']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE messages SET is_read = 1 WHERE target = %s AND sender = %s", (user, target))
    else:
        cur.execute("UPDATE messages SET is_read = 1 WHERE target = ? AND sender = ?", (user, target))
    conn.commit()
    conn.close()
    emit('messages_read', {'user': user, 'target': target}, room=target)

# --- СООБЩЕНИЯ И ИСТОРИЯ ЧАТА ---

@socketio.on('load_history')
def on_load_history(data):
    user = data['user']
    target = data['target']
    conn, db_type = get_db()
    cur = conn.cursor()
    
    pinned_msg = ""
    if target.startswith('#'):
        join_room(target)
        if db_type == 'postgres':
            cur.execute("SELECT pinned_msg FROM channels WHERE name = %s", (target,))
        else:
            cur.execute("SELECT pinned_msg FROM channels WHERE name = ?", (target,))
        p_row = cur.fetchone()
        if p_row: pinned_msg = p_row[0]

        if db_type == 'postgres':
            cur.execute("""
                SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited 
                FROM messages WHERE target = %s ORDER BY id ASC LIMIT 200
            """, (target,))
        else:
            cur.execute("""
                SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited 
                FROM messages WHERE target = ? ORDER BY id ASC LIMIT 200
            """, (target,))
    else:
        if db_type == 'postgres':
            cur.execute("""
                SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited 
                FROM messages 
                WHERE (sender = %s AND target = %s) OR (sender = %s AND target = %s) 
                ORDER BY id ASC LIMIT 200
            """, (user, target, target, user))
        else:
            cur.execute("""
                SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited 
                FROM messages 
                WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
                ORDER BY id ASC LIMIT 200
            """, (user, target, target, user))
        
    rows = cur.fetchall()
    conn.close()
    
    msgs = []
    for r in rows:
        msgs.append({
            'id': r[0], 'sender': r[1], 'target': r[2], 'msg': r[3],
            'msg_type': r[4], 'reply_to': r[5], 'reply_text': r[6], 'reactions': r[7], 'ttl': r[8], 'is_read': r[9], 'is_edited': r[10]
        })
    emit('history_loaded', {'messages': msgs, 'pinned_msg': pinned_msg})

@socketio.on('send_msg')
def on_send_msg(data):
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if db_type == 'postgres':
        cur.execute("""
            INSERT INTO messages (sender, target, message, msg_type, reply_to, reply_text, ttl) 
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (data['sender'], data['target'], data['msg'], data.get('msg_type', 'text'), data.get('reply_to'), data.get('reply_text', ''), data.get('ttl', 0)))
        msg_id = cur.fetchone()[0]
    else:
        cur.execute("""
            INSERT INTO messages (sender, target, message, msg_type, reply_to, reply_text, ttl) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data['sender'], data['target'], data['msg'], data.get('msg_type', 'text'), data.get('reply_to'), data.get('reply_text', ''), data.get('ttl', 0)))
        msg_id = cur.lastrowid
        
    conn.commit()
    conn.close()

    data['id'] = msg_id
    data['is_read'] = 0
    data['is_edited'] = 0
    target = data['target']
    sender = data['sender']
    
    if target.startswith('#'):
        emit('new_msg', data, room=target)
    else:
        emit('new_msg', data, room=target)
        if sender != target: emit('new_msg', data, room=sender)

        recipient_active_chat = user_active_chats.get(target)
        if recipient_active_chat != sender:
            display_text = data['msg']
            if data.get('msg_type') == 'voice': display_text = '🎤 Голосовое сообщение'
            elif data.get('msg_type') == 'video_note': display_text = '📹 Видео-сообщение'
            elif data.get('msg_type') == 'sticker': display_text = '🎨 Стикер'
            elif data.get('msg_type') == 'file': display_text = '📁 Файл'
            send_web_push(target, f"Сообщение от {sender}", display_text)

@socketio.on('edit_msg')
def on_edit_msg(data):
    msg_id = data['msg_id']
    new_text = data['new_text']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE messages SET message = %s, is_edited = 1 WHERE id = %s", (new_text, msg_id))
    else:
        cur.execute("UPDATE messages SET message = ?, is_edited = 1 WHERE id = ?", (new_text, msg_id))
    conn.commit()
    conn.close()
    emit('msg_edited', {'msg_id': msg_id, 'new_text': new_text}, broadcast=True)

@socketio.on('add_reaction')
def on_add_reaction(data):
    msg_id = data['msg_id']
    emoji = data['emoji']
    user = data['user']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("SELECT reactions FROM messages WHERE id = %s", (msg_id,))
    else:
        cur.execute("SELECT reactions FROM messages WHERE id = ?", (msg_id,))
    row = cur.fetchone()

    if row:
        reactions = json.loads(row[0] or '{}')
        if emoji not in reactions: reactions[emoji] = []
        if user in reactions[emoji]: reactions[emoji].remove(user)
        else: reactions[emoji].append(user)

        if db_type == 'postgres':
            cur.execute("UPDATE messages SET reactions = %s WHERE id = %s", (json.dumps(reactions), msg_id))
        else:
            cur.execute("UPDATE messages SET reactions = ? WHERE id = ?", (json.dumps(reactions), msg_id))
        conn.commit()
        conn.close()
        emit('update_msg_reactions', {'msg_id': msg_id, 'reactions': reactions}, broadcast=True)

@socketio.on('delete_msg')
def on_delete_msg(data):
    msg_id = data['msg_id']
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("DELETE FROM messages WHERE id = %s", (msg_id,))
    else:
        cur.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()
    emit('msg_deleted', {'msg_id': msg_id}, broadcast=True)

# --- СОБЫТИЯ ИГРЫ «МАФИЯ» ---

@socketio.on('webrtc_join')
def on_webrtc_join(data):
    room_id = data.get('room_id')
    join_room(room_id)
    emit('webrtc_user_joined', {'user': data['user'], 'sid': request.sid}, room=room_id, include_self=False)

@socketio.on('webrtc_signal')
def on_webrtc_signal(data):
    target_sid = data.get('target_sid')
    data['sender_sid'] = request.sid
    if target_sid: emit('webrtc_signal', data, room=target_sid)

def broadcast_room_state(room_id):
    room = mafia_rooms[room_id]
    emit('mafia_room_update', {
        'room_id': room.room_id,
        'name': room.name,
        'state': room.state,
        'players': room.players,
        'logs': room.logs,
        'timer_end': room.timer_end
    }, broadcast=True)

@socketio.on('mafia_get_rooms')
def on_mafia_get_rooms():
    rinfo = {}
    for rid, r in mafia_rooms.items(): rinfo[rid] = {'name': r.name, 'state': r.state, 'count': len(r.players)}
    emit('mafia_rooms_list', {'rooms': rinfo})

@socketio.on('mafia_get_room_state')
def on_mafia_get_room_state(data): broadcast_room_state(data['room_id'])

@socketio.on('mafia_join')
def on_mafia_join(data):
    rid = data['room_id']
    room = mafia_rooms[rid]
    if room.add_player(data['user']):
        room.logs.append(f"Игрок **{data['user']}** занял место.")
        broadcast_room_state(rid)

@socketio.on('mafia_leave')
def on_mafia_leave(data):
    rid = data['room_id']
    room = mafia_rooms[rid]
    if room.remove_player(data['user']):
        room.logs.append(f"Игрок **{data['user']}** покинул место.")
        broadcast_room_state(rid)

@socketio.on('mafia_start')
def on_mafia_start(data):
    rid = data['room_id']
    room = mafia_rooms[rid]
    ok, msg = room.start_game()
    if ok: broadcast_room_state(rid)

@socketio.on('mafia_action')
def on_mafia_action(data):
    rid = data['room_id']
    room = mafia_rooms[rid]
    user = data['user']
    target = data['target']
    act = data['action']

    if room.state == 'night':
        if act == 'kill' and room.players[user]['role'] == 'mafia': room.night_actions['mafia'][user] = target
        alive_mafia = [u for u, p in room.players.items() if p['alive'] and p['role'] == 'mafia']
        if len(room.night_actions['mafia']) >= len(alive_mafia):
            room.process_night()
            broadcast_room_state(rid)

    elif room.state == 'day':
        if act == 'vote':
            room.day_votes[user] = target
            alive_count = sum(1 for p in room.players.values() if p['alive'])
            if len(room.day_votes) >= alive_count:
                room.process_day()
                broadcast_room_state(rid)

# --- ТОЧКА ВХОДА СЕРВЕРА ---

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
