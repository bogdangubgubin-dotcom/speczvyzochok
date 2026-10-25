import os
import sqlite3
import hashlib
import time
import json
import random
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'speczvyazok_secret_key_2026'

socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=15 * 1024 * 1024, async_mode=None)

DATABASE_URL = os.environ.get('DATABASE_URL')

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
                members TEXT DEFAULT '',
                pinned_msg TEXT DEFAULT ''
            );
        """)
        cur.execute("INSERT INTO channels (name, creator, members) VALUES ('#general', 'system', '') ON CONFLICT (name) DO NOTHING;")
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
        try: cur.execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT ''")
        except sqlite3.OperationalError: pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                creator TEXT NOT NULL,
                members TEXT DEFAULT '',
                pinned_msg TEXT DEFAULT ''
            )
        """)
        cur.execute("INSERT OR IGNORE INTO channels (name, creator, members) VALUES ('#general', 'system', '')")
        try: cur.execute("ALTER TABLE channels ADD COLUMN pinned_msg TEXT DEFAULT ''")
        except sqlite3.OperationalError: pass

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
        try: cur.execute("ALTER TABLE messages ADD COLUMN ttl INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cur.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        try: cur.execute("ALTER TABLE messages ADD COLUMN is_edited INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass

        cur.execute("UPDATE users SET role = 'admin' WHERE username = 'totr'")

    conn.commit()
    conn.close()

init_db()

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

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

mafia_rooms = {f"room_{i}": MafiaRoom(f"room_{i}", f"Комната №{i}") for i in range(1, 6)}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Спецзвязочок</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        :root {
            --bg-color: #121212;
            --panel-bg: #1e1e1e;
            --panel-border: #2d2d2d;
            --text-color: #ffffff;
            --accent-color: #1F6AA5;
            --item-hover: #2a2a2a;
        }

        body.light-theme {
            --bg-color: #f4f5f7;
            --panel-bg: #ffffff;
            --panel-border: #e0e0e0;
            --text-color: #111111;
            --accent-color: #0078d4;
            --item-hover: #eef2f5;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        html, body { width: 100vw; height: 100dvh; overflow: hidden; background: var(--bg-color); color: var(--text-color); position: fixed; top: 0; left: 0; transition: background 0.3s, color 0.3s; }
        
        #auth-screen { display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100dvh; padding: 20px; }
        .auth-box { background: var(--panel-bg); padding: 25px; border-radius: 16px; width: 100%; max-width: 340px; text-align: center; border: 1px solid var(--panel-border); }
        .auth-box h2 { margin-bottom: 15px; color: #3B8ED0; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin: 8px 0; border-radius: 10px; border: 1px solid var(--panel-border); background: var(--bg-color); color: var(--text-color); outline: none; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none; background: var(--accent-color); color: #fff; font-weight: bold; cursor: pointer; font-size: 15px; }
        
        #app-screen { display: none; height: 100dvh; width: 100vw; position: relative; overflow: hidden; }
        
        #sidebar-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.6); z-index: 9; }
        #sidebar-overlay.active { display: block; }

        #sidebar { width: 280px; background: var(--panel-bg); border-right: 2px solid var(--panel-border); display: flex; flex-direction: column; flex-shrink: 0; z-index: 10; height: 100dvh; transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1); }
        .sidebar-header { padding: 12px 15px; background: var(--panel-bg); border-bottom: 1px solid var(--panel-border); font-weight: bold; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
        .section-title { font-size: 11px; text-transform: uppercase; color: #888; margin: 12px 8px 6px 8px; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        
        .chat-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; font-size: 14px; background: transparent; transition: background 0.2s; position: relative; }
        .chat-item:hover, .chat-item.active { background: var(--item-hover); }
        .chat-item.pinned-chat { border-left: 3px solid #3B8ED0; }
        
        .unread-badge { background: #e53935; color: #fff; font-size: 11px; font-weight: bold; border-radius: 10px; padding: 2px 7px; margin-left: auto; }

        .avatar { width: 34px; height: 34px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; font-size: 14px; flex-shrink: 0; background-size: cover; background-position: center; cursor: pointer; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-left: 4px; }
        .online { background: #4caf50; } .offline { background: #757575; }

        #chat-main { flex: 1; display: flex; flex-direction: column; background: var(--bg-color); min-width: 0; height: 100dvh; position: relative; }
        header { height: 56px; background: var(--panel-bg); padding: 0 12px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--panel-border); flex-shrink: 0; width: 100%; }
        .header-left { display: flex; align-items: center; gap: 8px; }
        .toggle-btn { width: 36px; height: 36px; background: var(--item-hover); border: 1px solid var(--panel-border); font-size: 18px; border-radius: 8px; color: var(--text-color); cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }

        #pinned-bar { display: none; background: var(--item-hover); padding: 6px 15px; border-bottom: 1px solid var(--panel-border); font-size: 13px; color: #3B8ED0; justify-content: space-between; align-items: center; }

        .search-bar { padding: 6px 12px; background: var(--panel-bg); border-bottom: 1px solid var(--panel-border); display: flex; gap: 8px; }
        .search-bar input { margin: 0; padding: 6px 10px; font-size: 13px; height: 32px; border-radius: 6px; width: 100%; }

        .chat-container { flex: 1; overflow-y: auto; padding: 15px 12px 75px 12px; display: flex; flex-direction: column; gap: 10px; -webkit-overflow-scrolling: touch; }
        
        .message { max-width: 85%; padding: 8px 12px; border-radius: 12px; font-size: 14px; position: relative; display: flex; flex-direction: column; gap: 3px; word-wrap: break-word; user-select: text; margin-top: 4px; }
        .msg-me { background: var(--accent-color); color: #fff; align-self: flex-end; border-bottom-right-radius: 2px; }
        .msg-other { background: var(--panel-bg); border: 1px solid var(--panel-border); align-self: flex-start; border-bottom-left-radius: 2px; }
        .msg-header { display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: bold; color: #82b1ff; }
        .msg-me .msg-header { justify-content: flex-end; color: #e0e0e0; }
        .msg-avatar { width: 18px; height: 18px; border-radius: 50%; background-size: cover; background-position: center; display: inline-block; cursor: pointer; }

        .video-note { width: 160px; height: 160px; border-radius: 12px; object-fit: cover; border: 2px solid var(--panel-border); background: #000; }

        .msg-status { font-size: 10px; align-self: flex-end; margin-top: 2px; opacity: 0.8; }
        .msg-me .msg-status { color: #bbdefb; }
        .msg-other .msg-status { color: #888; }

        .msg-actions { display: none; position: absolute; top: -26px; right: 0; background: var(--panel-bg); border-radius: 12px; padding: 3px 8px; gap: 6px; border: 1px solid var(--panel-border); z-index: 50; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
        .message:hover .msg-actions, .message:focus .msg-actions, .message.active-touch .msg-actions { display: flex; }
        .action-btn { cursor: pointer; font-size: 13px; padding: 2px 4px; color: var(--text-color); border: none; background: transparent; }
        .action-btn:hover { transform: scale(1.2); }

        .reactions-list { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
        .reaction-item { background: rgba(0,0,0,0.2); border-radius: 10px; padding: 2px 6px; font-size: 11px; border: 1px solid var(--panel-border); cursor: pointer; }

        .reply-preview-box { background: rgba(0,0,0,0.2); border-left: 3px solid #3B8ED0; padding: 4px 8px; border-radius: 4px; font-size: 12px; color: #aaa; margin-bottom: 4px; }
        
        #reply-bar { display: none; background: var(--panel-bg); padding: 4px 10px; border-top: 1px solid var(--panel-border); justify-content: space-between; align-items: center; font-size: 12px; color: #3B8ED0; }
        
        /* КРАСИВАЯ СТРOЧКА СООБЩЕНИЙ В ОДНУ ШИРИНКУ */
        footer { 
            position: absolute; 
            bottom: 0; 
            left: 0; 
            right: 0; 
            min-height: 56px; 
            background: var(--panel-bg); 
            padding: 6px 10px; 
            display: flex; 
            gap: 6px; 
            border-top: 1px solid var(--panel-border); 
            align-items: center; 
            justify-content: space-between;
            z-index: 99; 
        }
        .footer-left-btns, .footer-right-btns {
            display: flex;
            align-items: center;
            gap: 4px;
            flex-shrink: 0;
        }
        footer input[type="text"] { 
            flex: 1; 
            height: 38px; 
            background: var(--bg-color); 
            color: var(--text-color); 
            border: 1px solid var(--panel-border); 
            border-radius: 10px; 
            padding: 0 12px; 
            font-size: 15px; 
            outline: none; 
            min-width: 0; 
        }
        footer button#send-btn { 
            width: 38px; 
            height: 38px; 
            border-radius: 10px; 
            padding: 0; 
            flex-shrink: 0; 
            font-size: 16px; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            margin: 0;
        }
        .icon-btn { 
            background: var(--item-hover); 
            border: 1px solid var(--panel-border); 
            border-radius: 8px; 
            width: 34px; 
            height: 38px; 
            padding: 0; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            cursor: pointer; 
            color: var(--text-color); 
            flex-shrink: 0; 
            font-size: 14px; 
        }
        .recording { background: #c62828 !important; color: #fff !important; }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; padding: 15px; }
        .modal-box { background: var(--panel-bg); border: 1px solid var(--panel-border); border-radius: 12px; width: 100%; max-width: 480px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
        .modal-header { padding: 15px; background: var(--item-hover); border-bottom: 1px solid var(--panel-border); display: flex; justify-content: space-between; align-items: center; }
        .modal-body { padding: 15px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }

        .room-item { background: var(--item-hover); border: 1px solid var(--panel-border); border-radius: 10px; padding: 12px; display: flex; justify-content: space-between; align-items: center; }
        .room-title { font-weight: bold; font-size: 15px; }
        .room-info { font-size: 12px; color: #aaa; }
        
        .poker-table { background: #1b4332; border: 6px solid #2d6a4f; border-radius: 120px; padding: 15px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; justify-items: center; align-items: center; margin: 10px 0; }
        .table-slot { width: 90px; height: 110px; background: rgba(0,0,0,0.4); border: 2px dashed #40916c; border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 6px; text-align: center; position: relative; }
        .table-slot.occupied { border-style: solid; border-color: #52b788; background: rgba(0,0,0,0.7); }
        .slot-avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 16px; margin-bottom: 2px; background-size: cover; background-position: center; }
        .slot-name { font-size: 11px; font-weight: bold; color: #fff; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .slot-status { font-size: 10px; color: #aaa; }

        .audio-meter-box { width: 48px; height: 3px; background: #333; border-radius: 2px; overflow: hidden; margin: 2px 0; display: none; }
        .audio-meter-bar { height: 100%; width: 0%; background: #4caf50; transition: width 0.04s ease; }

        .logout-btn { background: transparent; border: 1px solid #555; color: #aaa; padding: 4px 8px; font-size: 12px; border-radius: 6px; cursor: pointer; width: auto; flex-shrink: 0; margin: 0; }
        .admin-nav-btn { background: #d32f2f; color: #fff; padding: 5px 8px; font-size: 11px; border-radius: 6px; border: none; font-weight: bold; cursor: pointer; display: none; }

        .avatar-upload-box { display: flex; flex-direction: column; align-items: center; gap: 10px; margin-bottom: 10px; }
        .big-avatar { width: 70px; height: 70px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: bold; color: #fff; background-size: cover; background-position: center; border: 2px solid #555; }

        #emoji-picker { display: none; position: absolute; bottom: 60px; left: 10px; background: var(--panel-bg); border: 1px solid var(--panel-border); border-radius: 12px; padding: 10px; grid-template-columns: repeat(5, 1fr); gap: 8px; z-index: 100; font-size: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
        #emoji-picker span { cursor: pointer; text-align: center; }
        #emoji-picker span:hover { transform: scale(1.3); }

        #lightbox { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.9); z-index: 2000; justify-content: center; align-items: center; }
        #lightbox img { max-width: 90%; max-height: 90%; border-radius: 8px; box-shadow: 0 0 20px rgba(0,0,0,0.8); }

        .media-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px; }
        .media-grid-item { width: 100%; height: 90px; border-radius: 8px; background-size: cover; background-position: center; cursor: pointer; border: 1px solid var(--panel-border); }

        .admin-user-row { display: flex; justify-content: space-between; align-items: center; background: var(--item-hover); padding: 8px 12px; border-radius: 8px; border: 1px solid var(--panel-border); font-size: 13px; }

        @media (max-width: 600px) {
            #sidebar { position: fixed; left: 0; top: 0; bottom: 0; width: 80%; max-width: 280px; transform: translateX(-100%); }
            #sidebar.open { transform: translateX(0); }
            .poker-table { grid-template-columns: repeat(2, 1fr); border-radius: 30px; }
            .video-note { width: 130px; height: 130px; }
        }
    </style>
</head>
<body>

    <div id="auth-screen">
        <div class="auth-box">
            <h2>Спецзвязочок</h2>
            <input type="text" id="username" placeholder="Логин">
            <input type="password" id="password" placeholder="Пароль">
            <button onclick="login()">Войти</button>
            <button onclick="register()" style="background: transparent; border: 1px solid #1F6AA5;">Регистрация</button>
            <div id="status" style="margin-top:10px; font-size:13px; color:#ff5555;"></div>
        </div>
    </div>

    <div id="app-screen">
        <div id="sidebar-overlay" onclick="closeSidebar()"></div>

        <div id="sidebar">
            <div class="sidebar-header">
                <div style="display:flex; align-items:center; gap:8px; cursor:pointer; overflow:hidden;" onclick="openProfileModal()">
                    <div id="my-avatar" class="avatar">?</div>
                    <span id="user-display" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:14px;">...</span>
                </div>
                <button class="logout-btn" onclick="logout()">Выйти</button>
            </div>
            <div class="chat-list">
                <div class="section-title">
                    <span>Каналы</span>
                    <button onclick="createChannelPrompt()" style="width:auto; padding:0 6px; height:20px; font-size:12px;">+</button>
                </div>
                <div id="channels-list"></div>
                <div class="section-title">Личные чаты</div>
                <div id="users-list"></div>
            </div>
        </div>

        <div id="chat-main" onclick="closeSidebar()">
            <header>
                <div class="header-left">
                    <button class="toggle-btn" onclick="event.stopPropagation(); toggleSidebar();">☰</button>
                    <div class="header-title-box">
                        <h3 id="chat-title" style="font-size:15px;">#general</h3>
                        <div id="typing-indicator" style="font-size:11px; color:#3B8ED0; height:13px;"></div>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap:6px;">
                    <button class="icon-btn" style="height:32px; width:auto; padding:0 8px; font-size:12px;" onclick="openGalleryModal()">🖼️ Медиа</button>
                    <button id="admin-btn" class="admin-nav-btn" onclick="openAdminModal()">⚙️ Админка</button>
                    <span style="font-size:10px; color:#4caf50;">🔒 E2EE</span>
                </div>
            </header>

            <div id="pinned-bar">
                <span>📌 <b id="pinned-msg-text">...</b></span>
                <button onclick="unpinMsg()" style="width:auto; padding:0 6px; background:transparent; border:none; color:#aaa; cursor:pointer;">✕</button>
            </div>

            <div class="search-bar">
                <input type="text" id="search-input" placeholder="🔍 Поиск по сообщениям..." oninput="searchMessages()">
            </div>

            <div class="chat-container" id="messages"></div>

            <div id="reply-bar">
                <span id="reply-text">Ответ...</span>
                <button onclick="cancelReply()" style="width:auto; padding:2px 8px; background:transparent; border:none; color:#aaa; cursor:pointer;">✕</button>
            </div>

            <div id="emoji-picker">
                <span onclick="addEmoji('👍')">👍</span><span onclick="addEmoji('❤️')">❤️</span><span onclick="addEmoji('🔥')">🔥</span>
                <span onclick="addEmoji('😂')">😂</span><span onclick="addEmoji('😮')">😮</span><span onclick="addEmoji('🎉')">🎉</span>
                <span onclick="addEmoji('💩')">💩</span><span onclick="addEmoji('👀')">👀</span><span onclick="addEmoji('💯')">💯</span>
                <span onclick="addEmoji('🤝')">🤝</span>
            </div>

            <footer>
                <div class="footer-left-btns">
                    <label class="icon-btn" title="Прикрепить файл">
                        📎
                        <input type="file" id="file-input" style="display:none;" onchange="sendFile()">
                    </label>
                    <button class="icon-btn" onclick="toggleEmojiPicker()" title="Смайлики">😀</button>
                </div>

                <input type="text" id="msg-input" placeholder="Сообщение..." oninput="notifyTyping()" onkeypress="if(event.key==='Enter') sendMsg()">

                <div class="footer-right-btns">
                    <button id="voice-record-btn" class="icon-btn" onclick="toggleVoiceRecord()" title="Голосовое">🎤</button>
                    <button id="video-record-btn" class="icon-btn" onclick="toggleVideoNoteRecord()" title="Видео-квадрат">📹</button>
                    <select id="ttl-select" class="icon-btn" style="padding:0; font-size:11px;" title="Таймер">
                        <option value="0">Off</option>
                        <option value="10">10s</option>
                        <option value="60">1m</option>
                    </select>
                    <button id="send-btn" onclick="sendMsg()">></button>
                </div>
            </footer>
        </div>
    </div>

    <!-- МЕДИА-ГАЛЕРЕЯ ЧАТА -->
    <div id="gallery-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>🖼️ Галерея медиафайлов</h3>
                <button onclick="closeGalleryModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div id="gallery-container" class="media-grid"></div>
            </div>
        </div>
    </div>

    <!-- КАРТОЧКА ПОЛЬЗОВАТЕЛЯ -->
    <div id="user-info-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>Карточка пользователя</h3>
                <button onclick="closeUserInfoModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body" style="text-align:center;">
                <div id="info-user-avatar" class="big-avatar" style="margin: 0 auto 10px auto;">?</div>
                <h3 id="info-user-name">...</h3>
                <p id="info-user-role" style="font-size:12px; color:#3B8ED0; margin-bottom:10px;">User</p>
                <button id="info-pm-btn" style="background:#1F6AA5;">💬 Написать в личку</button>
            </div>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО ПРОФИЛЯ -->
    <div id="profile-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>👤 Настройки Профиля</h3>
                <button onclick="closeProfileModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div class="avatar-upload-box">
                    <div id="profile-big-avatar" class="big-avatar" onclick="openLightbox(this.style.backgroundImage)">?</div>
                    <label style="background:#333; padding:6px 12px; border-radius:8px; border:1px solid #555; font-size:12px; cursor:pointer;">
                        📷 Изменить аватарку
                        <input type="file" id="avatar-input" accept="image/*" style="display:none;" onchange="uploadAvatar()">
                    </label>
                </div>

                <button onclick="toggleTheme()" style="background:#444; margin-bottom:5px;">🌗 Сменить тему оформления</button>
                <button onclick="openMafiaModal()" style="background:#8e24aa; margin-bottom:10px; padding:12px;">🕵️ Игра «Мафия»</button>
                
                <hr style="border:0; border-top:1px solid var(--panel-border); margin:10px 0;">
                <h4 style="font-size:14px; color:#aaa;">Смена пароля</h4>
                <input type="password" id="old-pass" placeholder="Старый пароль">
                <input type="password" id="new-pass" placeholder="Новый пароль">
                <button onclick="changePassword()" style="background:#2e7d32;">Сохранить новый пароль</button>
                <div id="pass-status" style="font-size:12px; text-align:center; color:#ff5555;"></div>
            </div>
        </div>
    </div>

    <!-- МОДАЛЬНОЕ ОКНО АДМИНКИ -->
    <div id="admin-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>⚙️ Панель Администратора</h3>
                <button onclick="closeAdminModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <p style="font-size:13px; color:#aaa; margin-bottom:10px;">Управление пользователями системы:</p>
                <div id="admin-users-list" style="display:flex; flex-direction:column; gap:8px;"></div>
            </div>
        </div>
    </div>

    <!-- ОКНО ИГРЫ МАФИЯ -->
    <div id="mafia-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3 id="mafia-modal-title">🕵️ Лобби комнат «Мафии»</h3>
                <button onclick="closeMafiaModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div id="mafia-rooms-screen">
                    <p style="font-size:13px; color:#aaa; margin-bottom:10px;">Выберите свободную комнату для игры:</p>
                    <div id="mafia-rooms-container" style="display:flex; flex-direction:column; gap:8px;"></div>
                </div>

                <div id="mafia-gameplay-screen" style="display:none;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <button onclick="backToMafiaRooms()" style="width:auto; padding:6px 12px; background:#444; font-size:12px;">← Назад</button>
                        <span id="room-phase-badge" style="background:#3B8ED0; padding:4px 8px; border-radius:6px; font-size:12px; font-weight:bold;">Лобби</span>
                        <span id="mafia-timer-badge" style="background:#d32f2f; padding:4px 8px; border-radius:6px; font-size:12px; font-weight:bold; display:none;">⏱ 60s</span>
                    </div>

                    <div class="poker-table" id="poker-table-slots"></div>

                    <div style="display:flex; justify-content:space-between; align-items:center; font-size:13px;">
                        <div>Моя роль: <b id="my-role-text">Скрыта</b> <button onclick="toggleRoleShow()" style="width:auto; padding:2px 6px; font-size:10px;">👁</button></div>
                        <button id="mic-toggle-btn" onclick="toggleMic()" style="width:auto; padding:4px 10px; font-size:11px; background:#c62828;">🔇 Микрофон ВЫКЛ</button>
                    </div>

                    <div style="display:flex; gap:8px; margin-top:5px;">
                        <button id="join-room-btn" onclick="joinActiveRoom()" style="background:#1F6AA5;">Занять место</button>
                        <button id="leave-room-btn" onclick="leaveActiveRoom()" style="background:#c62828; display:none;">Встать из-за стола</button>
                        <button onclick="startActiveRoomGame()" style="background:#2e7d32;">Старт игры</button>
                    </div>

                    <h4 style="font-size:13px; margin-top:5px;">События игры:</h4>
                    <div id="room-logs-box" style="background:#151515; border:1px solid #333; padding:8px; border-radius:8px; font-size:12px; height:90px; overflow-y:auto; color:#aaa;"></div>
                </div>
            </div>
        </div>
    </div>

    <div id="lightbox" onclick="this.style.display='none'"><img id="lightbox-img" src=""></div>
    <div id="audio-peers-container" style="display:none;"></div>

    <script>
        const socket = io();
        let currentUser = "";
        let currentTarget = "#general";
        let userRole = "user";
        let activeRoomId = null;
        let showRoleFlag = false;
        let mySecretRole = null;
        let editingMsgId = null;
        
        let isMicOn = false;
        let mediaStream = null;
        let audioContext = null;
        let analyser = null;
        let micAnimFrame = null;
        let peerConnections = {};

        let mediaRecorder = null;
        let videoRecorder = null;
        let audioChunks = [];
        let videoChunks = [];
        let isRecording = false;
        let isVideoRecording = false;
        let activeReplyMsg = null;
        let allLoadedMessages = {};
        let userAvatarsMap = {};
        let pinnedChats = JSON.parse(localStorage.getItem('pychat_pinned') || '[]');
        let unreadCounts = {};
        let typingTimeout = null;

        const iceServers = { 
            iceServers: [
                { urls: 'stun:stun.l.google.com:19302' },
                { urls: 'stun:stun1.l.google.com:19302' },
                { urls: 'turn:openrelay.metered.ca:80', username: 'openrelayproject', credential: 'openrelayproject' },
                { urls: 'turn:openrelay.metered.ca:443', username: 'openrelayproject', credential: 'openrelayproject' }
            ] 
        };

        if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();

        function playNotificationSound() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.connect(gain); gain.connect(ctx.destination);
                osc.frequency.value = 587.33;
                gain.gain.setValueAtTime(0.1, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
                osc.start(); osc.stop(ctx.currentTime + 0.3);
            } catch(e){}
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && !socket.connected) socket.connect();
        });

        function toggleTheme() {
            document.body.classList.toggle('light-theme');
            localStorage.setItem('pychat_theme', document.body.classList.contains('light-theme') ? 'light' : 'dark');
        }
        if (localStorage.getItem('pychat_theme') === 'light') document.body.classList.add('light-theme');

        function openLightbox(bgStyleUrl) {
            let url = bgStyleUrl.replace('url("', '').replace('")', '').replace("url('", '').replace("')", '');
            if (!url || url === 'none') return;
            document.getElementById('lightbox-img').src = url;
            document.getElementById('lightbox').style.display = 'flex';
        }

        function getAvatarColor(name) {
            let hash = 0;
            for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
            return '#' + ("00000" + (hash & 0x00FFFFFF).toString(16).toUpperCase()).slice(-6);
        }

        function setAvatarElement(elem, username) {
            const avatarUrl = userAvatarsMap[username];
            if (avatarUrl && avatarUrl.length > 10) {
                elem.style.backgroundImage = `url('${avatarUrl}')`;
                elem.innerText = '';
            } else {
                elem.style.backgroundImage = 'none';
                elem.style.backgroundColor = getAvatarColor(username);
                elem.innerText = username.charAt(0).toUpperCase();
            }
        }

        window.addEventListener('DOMContentLoaded', () => {
            const savedUser = localStorage.getItem('pychat_user');
            const savedPass = localStorage.getItem('pychat_pass');
            if (savedUser && savedPass) socket.emit('login', { username: savedUser, password: savedPass });
        });

        function login() {
            const u = document.getElementById('username').value.trim();
            const p = document.getElementById('password').value.trim();
            if (u && p) socket.emit('login', { username: u, password: p });
        }

        function register() {
            const u = document.getElementById('username').value.trim();
            const p = document.getElementById('password').value.trim();
            if (u && p) socket.emit('register', { username: u, password: p });
        }

        function logout() { localStorage.clear(); location.reload(); }

        socket.on('auth_response', (data) => {
            if (data.status === 'ok') {
                currentUser = data.username;
                userRole = data.role;
                localStorage.setItem('pychat_user', currentUser);
                localStorage.setItem('pychat_pass', document.getElementById('password').value || localStorage.getItem('pychat_pass'));
                document.getElementById('auth-screen').style.display = 'none';
                document.getElementById('app-screen').style.display = 'flex';
                document.getElementById('user-display').innerText = currentUser;

                if (userRole === 'admin' || currentUser === 'totr') {
                    document.getElementById('admin-btn').style.display = 'inline-block';
                } else {
                    document.getElementById('admin-btn').style.display = 'none';
                }

                if (data.avatar) userAvatarsMap[currentUser] = data.avatar;
                setAvatarElement(document.getElementById('my-avatar'), currentUser);
                setAvatarElement(document.getElementById('profile-big-avatar'), currentUser);

                socket.emit('get_channels');
                socket.emit('get_users');
                switchChat('#general');
            } else {
                document.getElementById('status').innerText = data.message;
            }
        });

        function createChannelPrompt() {
            const chName = prompt("Введите название нового канала (начиная с #):");
            if (chName && chName.startsWith('#')) {
                socket.emit('create_channel', { name: chName, creator: currentUser });
            } else if (chName) alert("Название должно начинаться с символа #");
        }

        function uploadAvatar() {
            const file = document.getElementById('avatar-input').files[0];
            if (!file) return;
            const img = new Image();
            const reader = new FileReader();
            reader.onload = function(e) {
                img.src = e.target.result;
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    const ctx = canvas.getContext('2d');
                    canvas.width = 200; canvas.height = 200;
                    ctx.drawImage(img, 0, 0, 200, 200);
                    const compressedB64 = canvas.toDataURL('image/jpeg', 0.8);
                    socket.emit('update_avatar', { user: currentUser, avatar: compressedB64 });
                };
            };
            reader.readAsDataURL(file);
        }

        socket.on('avatar_updated', (data) => {
            userAvatarsMap[data.user] = data.avatar;
            if (data.user === currentUser) {
                setAvatarElement(document.getElementById('my-avatar'), currentUser);
                setAvatarElement(document.getElementById('profile-big-avatar'), currentUser);
            }
            socket.emit('get_users');
        });

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('open');
            document.getElementById('sidebar-overlay').classList.toggle('active');
        }
        function closeSidebar() {
            document.getElementById('sidebar').classList.remove('open');
            document.getElementById('sidebar-overlay').classList.remove('active');
        }

        function switchChat(target) {
            currentTarget = target;
            unreadCounts[target] = 0;
            document.getElementById('chat-title').innerText = target.startsWith('#') ? target : (target === currentUser ? '📁 Сохраненные сообщения' : '@' + target);
            document.getElementById('messages').innerHTML = '';
            closeSidebar();
            socket.emit('load_history', { user: currentUser, target: currentTarget });
            socket.emit('get_users');
        }

        socket.on('history_loaded', (data) => {
            document.getElementById('messages').innerHTML = '';
            allLoadedMessages = {};
            data.messages.forEach(msg => appendMessage(msg));
            if (data.pinned_msg) showPinnedMsg(data.pinned_msg);
            else document.getElementById('pinned-bar').style.display = 'none';
            socket.emit('mark_read', { user: currentUser, target: currentTarget });
        });

        function showPinnedMsg(txt) {
            document.getElementById('pinned-msg-text').innerText = txt;
            document.getElementById('pinned-bar').style.display = 'flex';
        }

        function pinMsg(id) {
            const msg = allLoadedMessages[id];
            if (msg) socket.emit('pin_msg', { target: currentTarget, msg: msg.sender + ': ' + msg.msg });
        }

        function unpinMsg() { socket.emit('pin_msg', { target: currentTarget, msg: '' }); }

        socket.on('update_pinned', (data) => {
            if (data.target === currentTarget) {
                if (data.msg) showPinnedMsg(data.msg);
                else document.getElementById('pinned-bar').style.display = 'none';
            }
        });

        function togglePinChat(target) {
            if (pinnedChats.includes(target)) pinnedChats = pinnedChats.filter(c => c !== target);
            else pinnedChats.push(target);
            localStorage.setItem('pychat_pinned', JSON.stringify(pinnedChats));
            socket.emit('get_channels');
            socket.emit('get_users');
        }

        socket.on('channels_list', (data) => {
            const list = document.getElementById('channels-list');
            list.innerHTML = '';
            const sorted = [...data.channels].sort((a,b) => pinnedChats.includes(b) - pinnedChats.includes(a));
            sorted.forEach(ch => {
                const isPinned = pinnedChats.includes(ch);
                const unread = unreadCounts[ch] || 0;
                const div = document.createElement('div');
                div.className = 'chat-item' + (ch === currentTarget ? ' active' : '') + (isPinned ? ' pinned-chat' : '');
                div.innerHTML = `
                    <span>${ch}</span>
                    ${unread > 0 ? `<span class="unread-badge">${unread}</span>` : ''}
                    <button onclick="event.stopPropagation(); togglePinChat('${ch}')" style="width:auto; background:transparent; padding:0; border:none; font-size:12px; margin-left:4px;">📌</button>
                `;
                div.onclick = () => switchChat(ch);
                list.appendChild(div);
            });
        });

        socket.on('users_list', (data) => {
            const list = document.getElementById('users-list');
            list.innerHTML = '';
            
            const savedDiv = document.createElement('div');
            savedDiv.className = 'chat-item' + (currentTarget === currentUser ? ' active' : '');
            savedDiv.innerHTML = `<div class="avatar" style="background:#3B8ED0;">📁</div><span>Избранное</span>`;
            savedDiv.onclick = () => switchChat(currentUser);
            list.appendChild(savedDiv);

            const sortedUsers = [...data.users].sort((a,b) => pinnedChats.includes(b.username) - pinnedChats.includes(a.username));

            sortedUsers.forEach(u => {
                if (u.avatar) userAvatarsMap[u.username] = u.avatar;
                if (u.username === currentUser) return;

                const isPinned = pinnedChats.includes(u.username);
                const unread = unreadCounts[u.username] || 0;
                const div = document.createElement('div');
                div.className = 'chat-item' + (u.username === currentTarget ? ' active' : '') + (isPinned ? ' pinned-chat' : '');
                const avDiv = document.createElement('div');
                avDiv.className = 'avatar';
                setAvatarElement(avDiv, u.username);

                div.appendChild(avDiv);
                div.innerHTML += `
                    <span>${u.username}</span>
                    <span class="status-dot ${u.online ? 'online' : 'offline'}"></span>
                    ${unread > 0 ? `<span class="unread-badge">${unread}</span>` : ''}
                    <button onclick="event.stopPropagation(); togglePinChat('${u.username}')" style="width:auto; background:transparent; padding:0; border:none; font-size:12px; margin-left:4px;">📌</button>
                `;
                div.onclick = () => switchChat(u.username);
                list.appendChild(div);
            });
        });

        function openUserInfoModal(uname) {
            document.getElementById('info-user-name').innerText = uname;
            setAvatarElement(document.getElementById('info-user-avatar'), uname);
            document.getElementById('info-pm-btn').onclick = () => {
                closeUserInfoModal();
                switchChat(uname);
            };
            document.getElementById('user-info-modal').style.display = 'flex';
        }
        function closeUserInfoModal() { document.getElementById('user-info-modal').style.display = 'none'; }

        function openGalleryModal() {
            const container = document.getElementById('gallery-container');
            container.innerHTML = '';
            for (let id in allLoadedMessages) {
                const msg = allLoadedMessages[id];
                if (msg.msg_type === 'file') {
                    try {
                        const fileData = JSON.parse(msg.msg);
                        if (fileData.name.match(/\.(jpg|jpeg|png|gif|webp)$/i)) {
                            const item = document.createElement('div');
                            item.className = 'media-grid-item';
                            item.style.backgroundImage = `url('data:image/jpeg;base64,${fileData.data}')`;
                            item.onclick = () => openLightbox(`data:image/jpeg;base64,${fileData.data}`);
                            container.appendChild(item);
                        }
                    } catch(e){}
                }
            }
            document.getElementById('gallery-modal').style.display = 'flex';
        }
        function closeGalleryModal() { document.getElementById('gallery-modal').style.display = 'none'; }

        /* --- ФУНКЦИОНАЛ АДМИНКИ --- */
        function openAdminModal() {
            if (userRole !== 'admin' && currentUser !== 'totr') return;
            document.getElementById('admin-modal').style.display = 'flex';
            socket.emit('admin_get_users');
        }
        function closeAdminModal() { document.getElementById('admin-modal').style.display = 'none'; }

        socket.on('admin_users_data', (data) => {
            const container = document.getElementById('admin-users-list');
            container.innerHTML = '';
            data.users.forEach(u => {
                const row = document.createElement('div');
                row.className = 'admin-user-row';
                row.innerHTML = `
                    <div>
                        <b>${u.username}</b> <span style="font-size:11px; color:#aaa;">(${u.role})</span>
                    </div>
                    <div style="display:flex; gap:6px;">
                        <button onclick="adminSetRole('${u.username}', '${u.role === 'admin' ? 'user' : 'admin'}')" style="width:auto; padding:4px 8px; font-size:11px; background:#1F6AA5;">
                            ${u.role === 'admin' ? 'Сделать User' : 'Сделать Admin'}
                        </button>
                        ${u.username !== currentUser ? `<button onclick="adminDeleteUser('${u.username}')" style="width:auto; padding:4px 8px; font-size:11px; background:#d32f2f;">Удалить</button>` : ''}
                    </div>
                `;
                container.appendChild(row);
            });
        });

        function adminSetRole(uname, newRole) {
            socket.emit('admin_change_role', { admin: currentUser, target_user: uname, role: newRole });
        }

        function adminDeleteUser(uname) {
            if (confirm(`Вы уверены, что хотите удалить пользователя ${uname}?`)) {
                socket.emit('admin_delete_user', { admin: currentUser, target_user: uname });
            }
        }

        function changePassword() {
            const oldP = document.getElementById('old-pass').value.trim();
            const newP = document.getElementById('new-pass').value.trim();
            if (!oldP || !newP) return;
            socket.emit('change_password', { user: currentUser, old_pass: oldP, new_pass: newP });
        }

        socket.on('pass_response', (data) => {
            const st = document.getElementById('pass-status');
            st.innerText = data.message;
            st.style.color = data.status === 'ok' ? '#4caf50' : '#ff5555';
            if (data.status === 'ok') {
                localStorage.setItem('pychat_pass', document.getElementById('new-pass').value.trim());
                document.getElementById('old-pass').value = '';
                document.getElementById('new-pass').value = '';
            }
        });

        function notifyTyping() { socket.emit('user_typing', { sender: currentUser, target: currentTarget }); }

        socket.on('display_typing', (data) => {
            if (data.target === currentTarget || data.sender === currentTarget) {
                const ind = document.getElementById('typing-indicator');
                ind.innerText = data.sender + ' печатает...';
                clearTimeout(typingTimeout);
                typingTimeout = setTimeout(() => { ind.innerText = ''; }, 3000);
            }
        });

        function toggleEmojiPicker() {
            const picker = document.getElementById('emoji-picker');
            picker.style.display = picker.style.display === 'grid' ? 'none' : 'grid';
        }

        function addEmoji(emoji) {
            document.getElementById('msg-input').value += emoji;
            document.getElementById('emoji-picker').style.display = 'none';
        }

        function sendMsg() {
            const txt = document.getElementById('msg-input').value.trim();
            const ttl = parseInt(document.getElementById('ttl-select').value) || 0;
            if (!txt) return;

            if (editingMsgId) {
                socket.emit('edit_msg', { msg_id: editingMsgId, new_text: txt, user: currentUser });
                editingMsgId = null;
                document.getElementById('send-btn').innerText = '>';
            } else {
                socket.emit('send_msg', {
                    sender: currentUser, target: currentTarget, msg: txt, msg_type: 'text', ttl: ttl,
                    reply_to: activeReplyMsg ? activeReplyMsg.id : null,
                    reply_text: activeReplyMsg ? activeReplyMsg.sender + ': ' + activeReplyMsg.msg : ''
                });
            }
            document.getElementById('msg-input').value = '';
            cancelReply();
        }

        function startEditMsg(id) {
            const msg = allLoadedMessages[id];
            if (msg) {
                editingMsgId = id;
                document.getElementById('msg-input').value = msg.msg;
                document.getElementById('send-btn').innerText = '✓';
            }
        }

        socket.on('msg_edited', (data) => {
            if (allLoadedMessages[data.msg_id]) {
                allLoadedMessages[data.msg_id].msg = data.new_text;
                allLoadedMessages[data.msg_id].is_edited = 1;
                appendMessage(allLoadedMessages[data.msg_id]);
            }
        });

        function cancelReply() {
            activeReplyMsg = null;
            document.getElementById('reply-bar').style.display = 'none';
        }

        function sendFile() {
            const file = document.getElementById('file-input').files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = function(e) {
                const b64Data = e.target.result.split(',')[1];
                socket.emit('send_msg', {
                    sender: currentUser, target: currentTarget,
                    msg: JSON.stringify({ name: file.name, data: b64Data }), msg_type: 'file'
                });
                document.getElementById('file-input').value = '';
            };
            reader.readAsDataURL(file);
        }

        async function toggleVoiceRecord() {
            const btn = document.getElementById('voice-record-btn');
            if (!isRecording) {
                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    mediaRecorder = new MediaRecorder(stream);
                    audioChunks = [];
                    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
                    mediaRecorder.onstop = () => {
                        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                        const reader = new FileReader();
                        reader.onloadend = () => {
                            socket.emit('send_msg', {
                                sender: currentUser, target: currentTarget,
                                msg: reader.result.split(',')[1], msg_type: 'voice'
                            });
                        };
                        reader.readAsDataURL(audioBlob);
                    };
                    mediaRecorder.start();
                    isRecording = true;
                    btn.classList.add('recording');
                } catch (err) { alert('Микрофон недоступен: ' + err); }
            } else {
                mediaRecorder.stop();
                isRecording = false;
                btn.classList.remove('recording');
            }
        }

        async function toggleVideoNoteRecord() {
            const btn = document.getElementById('video-record-btn');
            if (!isVideoRecording) {
                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 300, height: 300 }, audio: true });
                    videoRecorder = new MediaRecorder(stream);
                    videoChunks = [];
                    videoRecorder.ondataavailable = e => videoChunks.push(e.data);
                    videoRecorder.onstop = () => {
                        const videoBlob = new Blob(videoChunks, { type: 'video/webm' });
                        const reader = new FileReader();
                        reader.onloadend = () => {
                            socket.emit('send_msg', {
                                sender: currentUser, target: currentTarget,
                                msg: reader.result.split(',')[1], msg_type: 'video_note'
                            });
                        };
                        reader.readAsDataURL(videoBlob);
                        stream.getTracks().forEach(t => t.stop());
                    };
                    videoRecorder.start();
                    isVideoRecording = true;
                    btn.classList.add('recording');
                } catch (err) { alert('Камера/микрофон недоступны: ' + err); }
            } else {
                videoRecorder.stop();
                isVideoRecording = false;
                btn.classList.remove('recording');
            }
        }

        socket.on('new_msg', (data) => { 
            if (data.sender !== currentUser) {
                playNotificationSound();
                if (data.target !== currentTarget && data.sender !== currentTarget) {
                    const chatKey = data.target.startsWith('#') ? data.target : data.sender;
                    unreadCounts[chatKey] = (unreadCounts[chatKey] || 0) + 1;
                    socket.emit('get_channels');
                    socket.emit('get_users');
                }
                if (document.hidden && Notification.permission === "granted") {
                    new Notification("Сообщение от " + data.sender, { body: data.msg });
                }
            }
            if (data.target === currentTarget || data.sender === currentTarget || (data.target.startsWith('#') && data.target === currentTarget)) {
                appendMessage(data); 
                socket.emit('mark_read', { user: currentUser, target: currentTarget });
            }
        });

        socket.on('messages_read', (data) => {
            if (data.target === currentUser || data.user === currentTarget) {
                document.querySelectorAll('.msg-status').forEach(st => st.innerText = '✓✓');
            }
        });

        function appendMessage(msg) {
            allLoadedMessages[msg.id] = msg;
            let div = document.getElementById('msg-elem-' + msg.id);
            const box = document.getElementById('messages');
            const isMe = msg.sender === currentUser;

            if (!div) {
                div = document.createElement('div');
                div.id = 'msg-elem-' + msg.id;
                div.className = 'message ' + (isMe ? 'msg-me' : 'msg-other');
                div.setAttribute('tabindex', '0');
                box.appendChild(div);
            }

            let replyHtml = msg.reply_text ? `<div class="reply-preview-box">${msg.reply_text}</div>` : '';
            let contentHtml = `<span>${msg.msg}</span>`;
            if (msg.msg_type === 'file') {
                try {
                    const fileData = JSON.parse(msg.msg);
                    if (fileData.name.match(/\.(jpg|jpeg|png|gif|webp)$/i)) {
                        contentHtml = `<img src="data:image/jpeg;base64,${fileData.data}" style="max-width:180px; border-radius:8px; cursor:pointer;" onclick="openLightbox(this.src)">`;
                    } else {
                        contentHtml = `<a href="data:application/octet-stream;base64,${fileData.data}" download="${fileData.name}" style="color:#fff;">📁 ${fileData.name}</a>`;
                    }
                } catch(e) {}
            } else if (msg.msg_type === 'voice') {
                contentHtml = `<audio controls playsinline src="data:audio/webm;base64,${msg.msg}" style="height:32px; width:170px;"></audio>`;
            } else if (msg.msg_type === 'video_note') {
                contentHtml = `<video class="video-note" controls playsinline src="data:video/webm;base64,${msg.msg}"></video>`;
            }

            let ttlBadge = msg.ttl > 0 ? `<span style="font-size:10px; color:#ff9800;"> 💣 (${msg.ttl}s)</span>` : '';
            let editBadge = msg.is_edited ? `<span style="font-size:10px; opacity:0.7;"> (изм.)</span>` : '';
            let readMark = isMe ? (msg.is_read ? '✓✓' : '✓') : '';

            let reactionsObj = {};
            try { reactionsObj = JSON.parse(msg.reactions || '{}'); } catch(e){}
            let reactionsHtml = '';
            for (const [emoji, users] of Object.entries(reactionsObj)) {
                if (users.length > 0) reactionsHtml += `<span class="reaction-item" onclick="addReaction(${msg.id}, '${emoji}')">${emoji} ${users.length}</span>`;
            }

            let deleteBtn = isMe ? `<button class="action-btn" onclick="deleteMessage(${msg.id})">🗑️</button>` : '';
            let editBtn = isMe && msg.msg_type === 'text' ? `<button class="action-btn" onclick="startEditMsg(${msg.id})">✏️</button>` : '';

            const avatarUrl = userAvatarsMap[msg.sender];
            let msgAvStyle = (avatarUrl && avatarUrl.length > 10) ? `background-image:url('${avatarUrl}')` : `background-color:${getAvatarColor(msg.sender)}`;

            div.innerHTML = `
                <div class="msg-actions">
                    <button class="action-btn" onclick="setReplyMsg(${msg.id})">↩️</button>
                    <button class="action-btn" onclick="pinMsg(${msg.id})">📌</button>
                    <button class="action-btn" onclick="addReaction(${msg.id}, '👍')">👍</button>
                    <button class="action-btn" onclick="addReaction(${msg.id}, '❤️')">❤️</button>
                    ${editBtn}
                    ${deleteBtn}
                </div>
                ${replyHtml}
                <div class="msg-header">
                    <span class="msg-avatar" style="${msgAvStyle}" onclick="openUserInfoModal('${msg.sender}')"></span>
                    <span onclick="openUserInfoModal('${msg.sender}')" style="cursor:pointer;">${isMe ? 'Вы' : msg.sender}${ttlBadge}</span>
                </div>
                ${contentHtml}${editBadge}
                <div class="msg-status">${readMark}</div>
                <div class="reactions-list">${reactionsHtml}</div>
            `;

            div.addEventListener('click', (e) => {
                e.stopPropagation();
                document.querySelectorAll('.message').forEach(m => m.classList.remove('active-touch'));
                div.classList.add('active-touch');
            });

            box.scrollTop = box.scrollHeight;
            if (msg.ttl > 0) setTimeout(() => { deleteMessage(msg.id); }, msg.ttl * 1000);
        }

        function setReplyMsg(id) {
            const targetMsg = allLoadedMessages[id];
            if (targetMsg) {
                activeReplyMsg = targetMsg;
                document.getElementById('reply-text').innerText = 'Ответ для ' + targetMsg.sender + ': ' + targetMsg.msg;
                document.getElementById('reply-bar').style.display = 'flex';
            }
        }

        function addReaction(id, emoji) { socket.emit('add_reaction', { msg_id: id, emoji: emoji, user: currentUser }); }

        socket.on('update_msg_reactions', (data) => {
            if (allLoadedMessages[data.msg_id]) {
                allLoadedMessages[data.msg_id].reactions = JSON.stringify(data.reactions);
                appendMessage(allLoadedMessages[data.msg_id]);
            }
        });

        function deleteMessage(id) { socket.emit('delete_msg', { msg_id: id, user: currentUser }); }

        socket.on('msg_deleted', (data) => {
            const elem = document.getElementById('msg-elem-' + data.msg_id);
            if (elem) elem.remove();
        });

        function searchMessages() {
            const query = document.getElementById('search-input').value.toLowerCase();
            document.querySelectorAll('.message').forEach(div => {
                const text = div.innerText.toLowerCase();
                div.style.display = text.includes(query) ? 'flex' : 'none';
            });
        }

        /* --- WEBRTC --- */
        function openMafiaModal() {
            closeProfileModal();
            document.getElementById('mafia-modal').style.display = 'flex';
            backToMafiaRooms();
        }
        function closeMafiaModal() { document.getElementById('mafia-modal').style.display = 'none'; }

        function backToMafiaRooms() {
            if (activeRoomId) leaveActiveRoom();
            activeRoomId = null;
            document.getElementById('mafia-modal-title').innerText = '🕵️ Лобби комнат «Мафии»';
            document.getElementById('mafia-rooms-screen').style.display = 'block';
            document.getElementById('mafia-gameplay-screen').style.display = 'none';
            socket.emit('mafia_get_rooms');
        }

        function enterMafiaRoom(roomId) {
            activeRoomId = roomId;
            document.getElementById('mafia-rooms-screen').style.display = 'none';
            document.getElementById('mafia-gameplay-screen').style.display = 'block';
            socket.emit('mafia_get_room_state', { room_id: activeRoomId });
        }

        function joinActiveRoom() { socket.emit('mafia_join', { room_id: activeRoomId, user: currentUser }); }
        function leaveActiveRoom() { stopMicMeter(); socket.emit('mafia_leave', { room_id: activeRoomId, user: currentUser }); }
        function startActiveRoomGame() { socket.emit('mafia_start', { room_id: activeRoomId }); }

        function toggleRoleShow() {
            showRoleFlag = !showRoleFlag;
            document.getElementById('my-role-text').innerText = showRoleFlag ? (mySecretRole || 'Нет роли') : 'Скрыта';
        }

        async function toggleMic() {
            isMicOn = !isMicOn;
            const btn = document.getElementById('mic-toggle-btn');
            if (isMicOn) {
                try {
                    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
                    btn.innerText = '🎙️ Микрофон ВКЛ'; btn.style.background = '#388e3c';
                    startMicMeter(mediaStream);
                    socket.emit('webrtc_join', { room_id: activeRoomId, user: currentUser });
                } catch(err) { alert('Микрофон недоступен: ' + err); isMicOn = false; }
            } else { stopMicMeter(); btn.innerText = '🔇 Микрофон ВЫКЛ'; btn.style.background = '#c62828'; }
        }

        function startMicMeter(stream) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
            if (audioContext.state === 'suspended') audioContext.resume();
            analyser = audioContext.createAnalyser();
            const source = audioContext.createMediaStreamSource(stream);
            source.connect(analyser);
            analyser.fftSize = 64;
            const dataArray = new Uint8Array(analyser.frequencyBinCount);
            const myMeterBox = document.getElementById('meter-box-' + currentUser);
            const myMeterBar = document.getElementById('meter-bar-' + currentUser);
            if (myMeterBox) myMeterBox.style.display = 'block';

            function updateLevel() {
                analyser.getByteFrequencyData(dataArray);
                let sum = 0;
                for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
                let average = sum / dataArray.length;
                let pct = Math.min(100, Math.round((average / 128) * 100));
                if (myMeterBar) myMeterBar.style.width = pct + '%';
                micAnimFrame = requestAnimationFrame(updateLevel);
            }
            updateLevel();
        }

        function createPeer(targetSid) {
            if (peerConnections[targetSid]) return peerConnections[targetSid];
            const pc = new RTCPeerConnection(iceServers);
            peerConnections[targetSid] = pc;
            if (mediaStream) mediaStream.getTracks().forEach(track => pc.addTrack(track, mediaStream));

            pc.onicecandidate = e => {
                if (e.candidate) socket.emit('webrtc_signal', { target_sid: targetSid, candidate: e.candidate });
            };

            pc.ontrack = e => {
                let audioElem = document.getElementById('audio-' + targetSid);
                if (!audioElem) {
                    audioElem = document.createElement('audio');
                    audioElem.id = 'audio-' + targetSid; audioElem.autoplay = true; audioElem.playsInline = true;
                    document.getElementById('audio-peers-container').appendChild(audioElem);
                }
                audioElem.srcObject = e.streams[0];
                audioElem.play().catch(err => {});
            };
            return pc;
        }

        socket.on('webrtc_user_joined', async (data) => {
            if (data.sid === socket.id) return;
            const pc = createPeer(data.sid);
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            socket.emit('webrtc_signal', { target_sid: data.sid, offer: offer });
        });

        socket.on('webrtc_signal', async (data) => {
            const senderSid = data.sender_sid;
            if (senderSid === socket.id) return;
            const pc = createPeer(senderSid);
            if (data.offer) {
                await pc.setRemoteDescription(new RTCSessionDescription(data.offer));
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);
                socket.emit('webrtc_signal', { target_sid: senderSid, answer: answer });
            } else if (data.answer) await pc.setRemoteDescription(new RTCSessionDescription(data.answer));
            else if (data.candidate) await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
        });

        function stopMicMeter() {
            if (micAnimFrame) cancelAnimationFrame(micAnimFrame);
            if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
            if (audioContext) audioContext.close();
            for (let sid in peerConnections) peerConnections[sid].close();
            peerConnections = {};
            document.getElementById('audio-peers-container').innerHTML = '';
            isMicOn = false;
            const myMeterBox = document.getElementById('meter-box-' + currentUser);
            if (myMeterBox) myMeterBox.style.display = 'none';
        }

        socket.on('mafia_rooms_list', (data) => {
            const container = document.getElementById('mafia-rooms-container');
            container.innerHTML = '';
            for (const [rid, rdata] of Object.entries(data.rooms)) {
                const item = document.createElement('div');
                item.className = 'room-item';
                item.innerHTML = `
                    <div>
                        <div class="room-title">${rdata.name}</div>
                        <div class="room-info">Игроков: ${rdata.count}/6 | Статус: ${rdata.state.toUpperCase()}</div>
                    </div>
                    <button style="width:auto; padding:6px 14px;" onclick="enterMafiaRoom('${rid}')">Войти</button>
                `;
                container.appendChild(item);
            }
        });

        socket.on('mafia_room_update', (data) => {
            if (data.room_id !== activeRoomId) return;
            document.getElementById('mafia-modal-title').innerText = '🕵️ ' + data.name;
            document.getElementById('room-phase-badge').innerText = data.state.toUpperCase();

            const timerBadge = document.getElementById('mafia-timer-badge');
            if (data.timer_end > 0) {
                const left = Math.max(0, data.timer_end - Math.floor(Date.now() / 1000));
                timerBadge.innerText = `⏱ ${left}s`;
                timerBadge.style.display = 'inline-block';
            } else timerBadge.style.display = 'none';

            if (data.players[currentUser]) {
                mySecretRole = data.players[currentUser].role;
                document.getElementById('join-room-btn').style.display = 'none';
                document.getElementById('leave-room-btn').style.display = 'inline-block';
            } else {
                document.getElementById('join-room-btn').style.display = 'inline-block';
                document.getElementById('leave-room-btn').style.display = 'none';
            }

            const table = document.getElementById('poker-table-slots');
            table.innerHTML = '';
            const playerBySlot = {};
            for (const [uname, pinfo] of Object.entries(data.players)) playerBySlot[pinfo.slot] = { name: uname, ...pinfo };

            for (let s = 0; s < 6; s++) {
                const slotDiv = document.createElement('div');
                if (playerBySlot[s]) {
                    const p = playerBySlot[s];
                    slotDiv.className = 'table-slot occupied';
                    let actBtn = '';
                    if (p.alive && p.name !== currentUser) {
                        if (data.state === 'night' && mySecretRole === 'mafia') actBtn = `<button class="act-btn" onclick="socket.emit('mafia_action', {room_id:'${activeRoomId}', user:'${currentUser}', target:'${p.name}', action:'kill'})">Убить</button>`;
                        if (data.state === 'day') actBtn = `<button class="act-btn" style="background:#f57c00;" onclick="socket.emit('mafia_action', {room_id:'${activeRoomId}', user:'${currentUser}', target:'${p.name}', action:'vote'})">Голос</button>`;
                    }
                    const avElem = document.createElement('div');
                    avElem.className = 'slot-avatar';
                    setAvatarElement(avElem, p.name);
                    slotDiv.appendChild(avElem);
                    slotDiv.innerHTML += `
                        <div class="slot-name">${p.name}</div>
                        <div class="audio-meter-box" id="meter-box-${p.name}"><div class="audio-meter-bar" id="meter-bar-${p.name}"></div></div>
                        <div class="slot-status">${p.alive ? '🟢 Жив' : '💀 Убит'}</div>
                        ${actBtn}
                    `;
                } else {
                    slotDiv.className = 'table-slot';
                    slotDiv.innerHTML = `<div class="slot-avatar" style="background:#333; color:#666;">+</div><div class="slot-name" style="color:#777;">Свободно</div>`;
                }
                table.appendChild(slotDiv);
            }

            const lbox = document.getElementById('room-logs-box');
            lbox.innerHTML = data.logs.join('<br>');
            lbox.scrollTop = lbox.scrollHeight;
        });

        function openProfileModal() { document.getElementById('profile-modal').style.display = 'flex'; }
        function closeProfileModal() { document.getElementById('profile-modal').style.display = 'none'; }
    </script>
</body>
</html>
"""

@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

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
        conn.close()
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            if username == 'totr':
                role = 'admin'
            join_room(username)
            join_room('#general')
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role, 'avatar': avatar})
        else: emit('auth_response', {'status': 'error', 'message': 'Неверный пароль'})
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
    chs = [r[0] for r in cur.fetchall()]
    conn.close()
    return chs

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

@socketio.on('get_channels')
def on_get_channels(): emit('channels_list', {'channels': get_all_channels()})

@socketio.on('get_users')
def on_get_users():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role, last_seen, avatar FROM users")
    users = [{'username': r[0], 'online': True, 'last_seen': r[2], 'avatar': r[3]} for r in cur.fetchall()]
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
    if target.startswith('#'): emit('new_msg', data, room=target)
    else:
        emit('new_msg', data, room=target)
        if data['sender'] != target: emit('new_msg', data, room=data['sender'])

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
