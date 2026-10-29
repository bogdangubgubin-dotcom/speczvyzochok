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

socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=25 * 1024 * 1024, async_mode=None, ping_timeout=10, ping_interval=5)

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
online_users = {}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Спецзвязочок Ultra</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#1F6AA5">
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

        .video-note { width: 160px; height: 160px; border-radius: 12px; object-fit: cover; border: 2px solid var(--panel-border); background: #000; cursor: pointer; }

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
        .footer-left-btns, .footer-right-btns { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
        footer input[type="text"] { flex: 1; height: 38px; background: var(--bg-color); color: var(--text-color); border: 1px solid var(--panel-border); border-radius: 10px; padding: 0 12px; font-size: 15px; outline: none; min-width: 0; }
        footer button#send-btn { width: 38px; height: 38px; border-radius: 10px; padding: 0; flex-shrink: 0; font-size: 16px; display: flex; align-items: center; justify-content: center; margin: 0; }
        .icon-btn { background: var(--item-hover); border: 1px solid var(--panel-border); border-radius: 8px; width: 34px; height: 38px; padding: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--text-color); flex-shrink: 0; font-size: 14px; }
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

        .sticker-img { width: 100px; height: 100px; object-fit: contain; cursor: pointer; }

        #lightbox { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.9); z-index: 2000; justify-content: center; align-items: center; }
        #lightbox img, #lightbox video { max-width: 90%; max-height: 90%; border-radius: 12px; box-shadow: 0 0 20px rgba(0,0,0,0.8); }

        .media-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 10px; }
        .media-grid-item { width: 100%; height: 90px; border-radius: 8px; background-size: cover; background-position: center; cursor: pointer; border: 1px solid var(--panel-border); }

        .admin-user-row { display: flex; justify-content: space-between; align-items: center; background: var(--item-hover); padding: 8px 12px; border-radius: 8px; border: 1px solid var(--panel-border); font-size: 13px; }

        #call-modal { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 3000; flex-direction: column; justify-content: center; align-items: center; gap: 20px; }
        .call-avatar { width: 100px; height: 100px; border-radius: 50%; font-size: 36px; display: flex; align-items: center; justify-content: center; border: 3px solid #3B8ED0; }

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
            <button type="button" onclick="login()">Войти</button>
            <button type="button" onclick="register()" style="background: transparent; border: 1px solid #1F6AA5;">Регистрация</button>
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
                        <div id="user-last-seen" style="font-size:10px; color:#aaa;"></div>
                        <div id="typing-indicator" style="font-size:11px; color:#3B8ED0; height:13px;"></div>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap:6px;">
                    <button id="call-btn" class="icon-btn" style="height:32px; width:36px; display:none;" onclick="startDirectCall()" title="Позвонить">📞</button>
                    <button class="icon-btn" style="height:32px; width:auto; padding:0 8px; font-size:12px;" onclick="openGalleryModal()">🖼️ Медиа</button>
                    <button id="admin-btn" class="admin-nav-btn" onclick="openAdminModal()">⚙️ Админка</button>
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
                <div style="display:flex; gap:10px; border-bottom:1px solid var(--panel-border); padding-bottom:5px; margin-bottom:5px; font-size:13px; font-weight:bold;">
                    <span onclick="switchPickerTab('emoji')" style="cursor:pointer; color:#3B8ED0;">😃 Смайлы</span>
                    <span onclick="switchPickerTab('stickers')" style="cursor:pointer; color:#3B8ED0;">🎨 Стикеры</span>
                </div>
                <div id="tab-emoji" style="display:grid; grid-template-columns: repeat(5, 1fr); gap:8px;">
                    <span onclick="addEmoji('👍')">👍</span><span onclick="addEmoji('❤️')">❤️</span><span onclick="addEmoji('🔥')">🔥</span>
                    <span onclick="addEmoji('😂')">😂</span><span onclick="addEmoji('😮')">😮</span><span onclick="addEmoji('🎉')">🎉</span>
                    <span onclick="addEmoji('💩')">💩</span><span onclick="addEmoji('👀')">👀</span><span onclick="addEmoji('💯')">💯</span>
                    <span onclick="addEmoji('🤝')">🤝</span>
                </div>
                <div id="tab-stickers" style="display:none; flex-wrap:wrap; gap:8px;">
                    <span onclick="sendSticker('https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExcDFnYnJndXdyOXptc2o2NWt0Yjl4eWk5bzIxeWpjdHltcXlsNm0xbSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9cw/cJuYQ8A12UagY/giphy.gif')">🔥</span>
                    <span onclick="sendSticker('https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExbmFnajA1OTdyMHg1eWpxZHRjMWs2YmlmN3ltMXdqY2M3bmtsczFkNSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9cw/l3vR85PnGiHGlHO8w/giphy.gif')">🐱</span>
                </div>
            </div>

            <footer>
                <div class="footer-left-btns">
                    <label class="icon-btn" title="Прикрепить файл">
                        📎
                        <input type="file" id="file-input" style="display:none;" onchange="sendFile()">
                    </label>
                    <button class="icon-btn" onclick="toggleEmojiPicker()" title="Смайлы и стикеры">😀</button>
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

    <!-- ОКНО ГОЛОСОВОГО ЗВОНКА -->
    <div id="call-modal">
        <div id="call-user-avatar" class="call-avatar">?</div>
        <h3 id="call-user-name">Звонок...</h3>
        <p id="call-status-text" style="color:#aaa; font-size:14px;">Вызов...</p>
        <div style="display:flex; gap:15px;">
            <button id="accept-call-btn" style="background:#2e7d32; width:auto; padding:10px 20px; display:none;" onclick="acceptCall()">Принять</button>
            <button style="background:#c62828; width:auto; padding:10px 20px;" onclick="endCall()">Сбросить</button>
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
                <div id="info-user-avatar" class="big-avatar" style="margin:0 auto 10px auto;">?</div>
                <h3 id="info-user-name">...</h3>
                <p id="info-user-role" style="font-size:12px; color:#3B8ED0; margin-bottom:10px;">User</p>
                <button id="info-pm-btn" style="background:#1F6AA5;">💬 Написать в личку</button>
            </div>
        </div>
    </div>

    <!-- ПРОФИЛЬ -->
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
                <button onclick="requestPushPermission()" style="background:#1F6AA5; margin-bottom:5px; padding:12px;">🔔 Разрешить Push-уведомления</button>
                <button onclick="toggleTheme()" style="background:#444; margin-bottom:5px;">🌗 Сменить тему</button>
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

    <!-- АДМИНКА -->
    <div id="admin-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>⚙️ Панель Администратора</h3>
                <button onclick="closeAdminModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div id="admin-users-list" style="display:flex; flex-direction:column; gap:8px;"></div>
            </div>
        </div>
    </div>

    <!-- МОДАЛКА МАФИИ -->
    <div id="mafia-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3 id="mafia-modal-title">🕵️ Лобби комнат «Мафии»</h3>
                <button onclick="closeMafiaModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div id="mafia-rooms-screen">
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
                    <div id="room-logs-box" style="background:#151515; border:1px solid #333; padding:8px; border-radius:8px; font-size:12px; height:90px; overflow-y:auto; color:#aaa; margin-top:5px;"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- ЛАЙТБОКС (ФОТО И ВИДЕО ПОЛНОЭКРАННО) -->
    <div id="lightbox" onclick="closeLightbox()">
        <img id="lightbox-img" src="" style="display:none;">
        <video id="lightbox-video" controls autoplay loop playsinline style="display:none; max-width:90%; max-height:90%; border-radius:12px;"></video>
    </div>

    <div id="audio-peers-container" style="display:none;"></div>
    <audio id="direct-call-audio" autoplay playsinline style="display:none;"></audio>

<script>
let socket;
try {
    socket = io({reconnection:true, reconnectionAttempts:Infinity, reconnectionDelay:1000, reconnectionDelayMax:5000, timeout:20000});
} catch(e) {
    console.error("SocketIO не загружен:", e);
}

let currentUser="", currentTarget="#general", userRole="user", activeRoomId=null, showRoleFlag=false, mySecretRole=null, editingMsgId=null, isMicOn=false, mediaStream=null, audioContext=null, analyser=null, micAnimFrame=null, peerConnections={}, mediaRecorder=null, videoRecorder=null, audioChunks=[], videoChunks=[], isRecording=false, isVideoRecording=false, activeReplyMsg=null, allLoadedMessages={}, userAvatarsMap={}, pinnedChats=JSON.parse(localStorage.getItem('pychat_pinned')||'[]'), unreadCounts={}, typingTimeout=null, swRegistration=null, activeCallPeer=null, directCallStream=null, incomingCallData=null;

const iceServers={iceServers:[{urls:'stun:stun.l.google.com:19302'},{urls:'stun:stun1.l.google.com:19302'}]};

if('serviceWorker' in navigator){
    navigator.serviceWorker.register('/sw.js').then(r=>{swRegistration=r;}).catch(e=>{});
}

function requestPushPermission(){
    if("Notification" in window){
        if(Notification.permission!=="granted"){
            Notification.requestPermission().then(p=>{if(p==="granted") alert("✅ Push разрешены!"); else alert("⚠️ Доступ отклонен.");});
        }
    }
}

function triggerSystemNotification(s,t){
    if(!("Notification" in window)||Notification.permission!=="granted") return;
    const txt = t.length>50 ? t.substring(0,50)+"..." : t;
    if(swRegistration&&swRegistration.showNotification){
        swRegistration.showNotification("Сообщение от "+s,{body:txt, icon:'https://cdn-icons-png.flaticon.com/512/732/732200.png'});
    } else {
        new Notification("Сообщение от "+s,{body:txt});
    }
}

function playNotificationSound(){
    try {
        const c=new(window.AudioContext||window.webkitAudioContext)(), o=c.createOscillator(), g=c.createGain();
        o.connect(g); g.connect(c.destination); o.frequency.value=587.33; g.gain.setValueAtTime(0.1,c.currentTime);
        g.gain.exponentialRampToValueAtTime(0.001,c.currentTime+0.3); o.start(); o.stop(c.currentTime+0.3);
    } catch(e){}
}

function toggleTheme(){
    document.body.classList.toggle('light-theme');
    localStorage.setItem('pychat_theme', document.body.classList.contains('light-theme')?'light':'dark');
}
if(localStorage.getItem('pychat_theme')==='light') document.body.classList.add('light-theme');

function openLightbox(u){
    let url = u.replace('url("','').replace('")','').replace("url('",'').replace("')",'');
    if(!url||url==='none') return;
    document.getElementById('lightbox-video').style.display='none';
    document.getElementById('lightbox-img').src=url;
    document.getElementById('lightbox-img').style.display='block';
    document.getElementById('lightbox').style.display='flex';
}

function openVideoLightbox(s){
    document.getElementById('lightbox-img').style.display='none';
    const v=document.getElementById('lightbox-video');
    v.src=s;
    v.style.display='block';
    document.getElementById('lightbox').style.display='flex';
}

function closeLightbox(){
    document.getElementById('lightbox').style.display='none';
    document.getElementById('lightbox-video').pause();
}

function getAvatarColor(n){
    let h=0; for(let i=0;i<n.length;i++) h=n.charCodeAt(i)+((h<<5)-h);
    return '#'+("00000"+(h&0x00FFFFFF).toString(16).toUpperCase()).slice(-6);
}

function setAvatarElement(e,u){
    const url=userAvatarsMap[u];
    if(url&&url.length>10){
        e.style.backgroundImage='url("'+url+'")'; e.innerText='';
    } else {
        e.style.backgroundImage='none'; e.style.backgroundColor=getAvatarColor(u); e.innerText=u.charAt(0).toUpperCase();
    }
}

window.addEventListener('DOMContentLoaded',()=>{
    const u=localStorage.getItem('pychat_user'), p=localStorage.getItem('pychat_pass');
    if(u&&p&&socket) socket.emit('login',{username:u,password:p});
});

function login(){
    const u=document.getElementById('username').value.trim(), p=document.getElementById('password').value.trim();
    if(!u || !p) {
        document.getElementById('status').innerText = "Заполните логин и пароль!";
        return;
    }
    if(socket) socket.emit('login',{username:u,password:p});
}

function register(){
    const u=document.getElementById('username').value.trim(), p=document.getElementById('password').value.trim();
    if(!u || !p) {
        document.getElementById('status').innerText = "Заполните логин и пароль!";
        return;
    }
    if(socket) socket.emit('register',{username:u,password:p});
}

function logout(){ localStorage.clear(); location.reload(); }

if(socket) {
    socket.on('auth_response',d=>{
        if(d.status==='ok'){
            currentUser=d.username; userRole=d.role;
            localStorage.setItem('pychat_user',currentUser);
            localStorage.setItem('pychat_pass',document.getElementById('password').value||localStorage.getItem('pychat_pass'));
            document.getElementById('auth-screen').style.display='none';
            document.getElementById('app-screen').style.display='flex';
            document.getElementById('user-display').innerText=currentUser;
            document.getElementById('admin-btn').style.display=(userRole==='admin'||currentUser==='totr')?'inline-block':'none';
            if(d.avatar) userAvatarsMap[currentUser]=d.avatar;
            setAvatarElement(document.getElementById('my-avatar'),currentUser);
            setAvatarElement(document.getElementById('profile-big-avatar'),currentUser);
            socket.emit('get_channels'); socket.emit('get_users'); switchChat('#general');
        } else {
            document.getElementById('status').innerText=d.message;
        }
    });
}

function createChannelPrompt(){
    const n=prompt("Введите имя канала (#):");
    if(n&&n.startsWith('#')) socket.emit('create_channel',{name:n,creator:currentUser});
}

function uploadAvatar(){
    const f=document.getElementById('avatar-input').files[0]; if(!f) return;
    const i=new Image(), r=new FileReader();
    r.onload=e=>{
        i.src=e.target.result;
        i.onload=()=>{
            const c=document.createElement('canvas'), ctx=c.getContext('2d');
            c.width=200; c.height=200; ctx.drawImage(i,0,0,200,200);
            socket.emit('update_avatar',{user:currentUser,avatar:c.toDataURL('image/jpeg',0.8)});
        };
    };
    r.readAsDataURL(f);
}

if(socket) {
    socket.on('avatar_updated',d=>{
        userAvatarsMap[d.user]=d.avatar;
        if(d.user===currentUser){
            setAvatarElement(document.getElementById('my-avatar'),currentUser);
            setAvatarElement(document.getElementById('profile-big-avatar'),currentUser);
        }
        socket.emit('get_users');
    });
}

function toggleSidebar(){ document.getElementById('sidebar').classList.toggle('open'); document.getElementById('sidebar-overlay').classList.toggle('active'); }
function closeSidebar(){ document.getElementById('sidebar').classList.remove('open'); document.getElementById('sidebar-overlay').classList.remove('active'); }

function switchChat(t){
    currentTarget=t; unreadCounts[t]=0; const isG=t.startsWith('#');
    document.getElementById('chat-title').innerText=isG?t:(t===currentUser?'📁 Избранное':'@'+t);
    document.getElementById('call-btn').style.display=(!isG&&t!==currentUser)?'inline-block':'none';
    document.getElementById('messages').innerHTML=''; closeSidebar();
    if(socket) {
        socket.emit('load_history',{user:currentUser,target:currentTarget});
        socket.emit('get_users');
    }
}

if(socket) {
    socket.on('history_loaded',d=>{
        document.getElementById('messages').innerHTML=''; allLoadedMessages={};
        d.messages.forEach(m=>appendMessage(m));
        if(d.pinned_msg) showPinnedMsg(d.pinned_msg); else document.getElementById('pinned-bar').style.display='none';
        socket.emit('mark_read',{user:currentUser,target:currentTarget});
    });
}

function showPinnedMsg(t){ document.getElementById('pinned-msg-text').innerText=t; document.getElementById('pinned-bar').style.display='flex'; }
function pinMsg(id){ const m=allLoadedMessages[id]; if(m) socket.emit('pin_msg',{target:currentTarget,msg:m.sender+': '+m.msg}); }
function unpinMsg(){ socket.emit('pin_msg',{target:currentTarget,msg:''}); }

if(socket) {
    socket.on('update_pinned',d=>{
        if(d.target===currentTarget){
            if(d.msg) showPinnedMsg(d.msg); else document.getElementById('pinned-bar').style.display='none';
        }
    });
}

function togglePinChat(t){
    if(pinnedChats.includes(t)) pinnedChats=pinnedChats.filter(c=>c!==t); else pinnedChats.push(t);
    localStorage.setItem('pychat_pinned',JSON.stringify(pinnedChats));
    if(socket) { socket.emit('get_channels'); socket.emit('get_users'); }
}

if(socket) {
    socket.on('channels_list',d=>{
        const l=document.getElementById('channels-list'); l.innerHTML='';
        [...d.channels].sort((a,b)=>pinnedChats.includes(b.name)-pinnedChats.includes(a.name)).forEach(c=>{
            const div=document.createElement('div');
            div.className='chat-item'+(c.name===currentTarget?' active':'')+(pinnedChats.includes(c.name)?' pinned-chat':'');
            div.innerHTML='<span>'+c.name+'</span>'+((unreadCounts[c.name]||0)>0?'<span class="unread-badge">'+unreadCounts[c.name]+'</span>':'')+'<button onclick="event.stopPropagation();togglePinChat(\''+c.name+'\')" style="width:auto;background:transparent;padding:0;border:none;font-size:12px;margin-left:4px;">📌</button>';
            div.onclick=()=>switchChat(c.name); l.appendChild(div);
        });
    });

    socket.on('users_list',d=>{
        const l=document.getElementById('users-list'); l.innerHTML='';
        const s=document.createElement('div'); s.className='chat-item'+(currentTarget===currentUser?' active':'');
        s.innerHTML='<div class="avatar" style="background:#3B8ED0;">📁</div><span>Избранное</span>';
        s.onclick=()=>switchChat(currentUser); l.appendChild(s);
        [...d.users].sort((a,b)=>pinnedChats.includes(b.username)-pinnedChats.includes(a.username)).forEach(u=>{
            if(u.avatar) userAvatarsMap[u.username]=u.avatar;
            if(u.username===currentUser) return;
            if(u.username===currentTarget) document.getElementById('user-last-seen').innerText=u.online?'🟢 В сети':'Был(а) в сети: '+(u.last_seen?new Date(u.last_seen*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'недавно');
            const div=document.createElement('div');
            div.className='chat-item'+(u.username===currentTarget?' active':'')+(pinnedChats.includes(u.username)?' pinned-chat':'');
            const av=document.createElement('div'); av.className='avatar'; setAvatarElement(av,u.username);
            div.appendChild(av);
            div.innerHTML+='<span>'+u.username+'</span><span class="status-dot '+(u.online?'online':'offline')+'"></span>'+((unreadCounts[u.username]||0)>0?'<span class="unread-badge">'+unreadCounts[u.username]+'</span>':'')+'<button onclick="event.stopPropagation();togglePinChat(\''+u.username+'\')" style="width:auto;background:transparent;padding:0;border:none;font-size:12px;margin-left:4px;">📌</button>';
            div.onclick=()=>switchChat(u.username); l.appendChild(div);
        });
    });
}

function openUserInfoModal(u){
    document.getElementById('info-user-name').innerText=u;
    setAvatarElement(document.getElementById('info-user-avatar'),u);
    document.getElementById('info-pm-btn').onclick=()=>{closeUserInfoModal(); switchChat(u);};
    document.getElementById('user-info-modal').style.display='flex';
}
function closeUserInfoModal(){ document.getElementById('user-info-modal').style.display='none'; }

function openGalleryModal(){
    const c=document.getElementById('gallery-container'); c.innerHTML='';
    for(let id in allLoadedMessages){
        const m=allLoadedMessages[id];
        if(m.msg_type==='file'){
            try {
                const f=JSON.parse(m.msg);
                if(f.name.match(/\.(jpg|jpeg|png|gif|webp)$/i)){
                    const it=document.createElement('div'); it.className='media-grid-item';
                    it.style.backgroundImage='url("data:image/jpeg;base64,'+f.data+'")';
                    it.onclick=()=>openLightbox('data:image/jpeg;base64,'+f.data); c.appendChild(it);
                }
            } catch(e){}
        }
    }
    document.getElementById('gallery-modal').style.display='flex';
}
function closeGalleryModal(){ document.getElementById('gallery-modal').style.display='none'; }

function openAdminModal(){ if(userRole!=='admin'&&currentUser!=='totr') return; document.getElementById('admin-modal').style.display='flex'; socket.emit('admin_get_users'); }
function closeAdminModal(){ document.getElementById('admin-modal').style.display='none'; }

if(socket) {
    socket.on('admin_users_data',d=>{
        const c=document.getElementById('admin-users-list'); c.innerHTML='';
        d.users.forEach(u=>{
            const r=document.createElement('div'); r.className='admin-user-row';
            r.innerHTML='<div><b>'+u.username+'</b> <span style="font-size:11px;color:#aaa;">('+u.role+')</span></div><div style="display:flex;gap:6px;"><button onclick="adminSetRole(\''+u.username+'\',\''+(u.role==='admin'?'user':'admin')+'\')" style="width:auto;padding:4px 8px;font-size:11px;background:#1F6AA5;">'+(u.role==='admin'?'User':'Admin')+'</button>'+(u.username!==currentUser?'<button onclick="adminDeleteUser(\''+u.username+'\')" style="width:auto;padding:4px 8px;font-size:11px;background:#d32f2f;">Удалить</button>':'')+'</div>';
            c.appendChild(r);
        });
    });
}

function adminSetRole(u,r){ socket.emit('admin_change_role',{admin:currentUser,target_user:u,role:r}); }
function adminDeleteUser(u){ if(confirm("Удалить "+u+"?")) socket.emit('admin_delete_user',{admin:currentUser,target_user:u}); }

function changePassword(){
    const o=document.getElementById('old-pass').value.trim(), n=document.getElementById('new-pass').value.trim();
    if(o&&n) socket.emit('change_password',{user:currentUser,old_pass:o,new_pass:n});
}

if(socket) {
    socket.on('pass_response',d=>{
        const s=document.getElementById('pass-status'); s.innerText=d.message; s.style.color=d.status==='ok'?'#4caf50':'#ff5555';
        if(d.status==='ok'){
            localStorage.setItem('pychat_pass',document.getElementById('new-pass').value.trim());
            document.getElementById('old-pass').value=''; document.getElementById('new-pass').value='';
        }
    });
}

function notifyTyping(){ socket.emit('user_typing',{sender:currentUser,target:currentTarget}); }

if(socket) {
    socket.on('display_typing',d=>{
        if(d.target===currentTarget||d.sender===currentTarget){
            const i=document.getElementById('typing-indicator'); i.innerText=d.sender+' печатает...';
            clearTimeout(typingTimeout); typingTimeout=setTimeout(()=>{i.innerText='';},3000);
        }
    });
}

function toggleEmojiPicker(){ const p=document.getElementById('emoji-picker'); p.style.display=p.style.display==='block'?'none':'block'; }
function switchPickerTab(t){ document.getElementById('tab-emoji').style.display=t==='emoji'?'grid':'none'; document.getElementById('tab-stickers').style.display=t==='stickers'?'flex':'none'; }
function addEmoji(e){ document.getElementById('msg-input').value+=e; document.getElementById('emoji-picker').style.display='none'; }
function sendSticker(u){ socket.emit('send_msg',{sender:currentUser,target:currentTarget,msg:u,msg_type:'sticker'}); document.getElementById('emoji-picker').style.display='none'; }

function sendMsg(){
    if(!socket.connected) socket.connect();
    const t=document.getElementById('msg-input').value.trim(), ttl=parseInt(document.getElementById('ttl-select').value)||0;
    if(!t) return;
    if(editingMsgId){
        socket.emit('edit_msg',{msg_id:editingMsgId,new_text:t,user:currentUser});
        editingMsgId=null; document.getElementById('send-btn').innerText='>';
    } else {
        socket.emit('send_msg',{sender:currentUser,target:currentTarget,msg:t,msg_type:'text',ttl:ttl,reply_to:activeReplyMsg?activeReplyMsg.id:null,reply_text:activeReplyMsg?activeReplyMsg.sender+': '+activeReplyMsg.msg:''});
    }
    document.getElementById('msg-input').value=''; cancelReply();
}

function startEditMsg(id){ const m=allLoadedMessages[id]; if(m){ editingMsgId=id; document.getElementById('msg-input').value=m.msg; document.getElementById('send-btn').innerText='✓'; } }
if(socket) { socket.on('msg_edited',d=>{ if(allLoadedMessages[d.msg_id]){ allLoadedMessages[d.msg_id].msg=d.new_text; allLoadedMessages[d.msg_id].is_edited=1; appendMessage(allLoadedMessages[d.msg_id]); } }); }
function cancelReply(){ activeReplyMsg=null; document.getElementById('reply-bar').style.display='none'; }

function sendFile(){
    const f=document.getElementById('file-input').files[0]; if(!f) return;
    const r=new FileReader(); r.onload=e=>{
        socket.emit('send_msg',{sender:currentUser,target:currentTarget,msg:JSON.stringify({name:f.name,data:e.target.result.split(',')[1]}),msg_type:'file'});
        document.getElementById('file-input').value='';
    };
    r.readAsDataURL(f);
}

async function toggleVoiceRecord(){
    const b=document.getElementById('voice-record-btn');
    if(!isRecording){
        try {
            const s=await navigator.mediaDevices.getUserMedia({audio:true});
            mediaRecorder=new MediaRecorder(s); audioChunks=[];
            mediaRecorder.ondataavailable=e=>audioChunks.push(e.data);
            mediaRecorder.onstop=()=>{
                const r=new FileReader(); r.onloadend=()=>socket.emit('send_msg',{sender:currentUser,target:currentTarget,msg:r.result.split(',')[1],msg_type:'voice'});
                r.readAsDataURL(new Blob(audioChunks,{type:'audio/webm'}));
            };
            mediaRecorder.start(); isRecording=true; b.classList.add('recording');
        } catch(e){ alert('Ошибка микрофона!'); }
    } else {
        mediaRecorder.stop(); isRecording=false; b.classList.remove('recording');
    }
}

async function toggleVideoNoteRecord(){
    const b=document.getElementById('video-record-btn');
    if(!isVideoRecording){
        try {
            const s=await navigator.mediaDevices.getUserMedia({video:{width:300,height:300},audio:true});
            videoRecorder=new MediaRecorder(s); videoChunks=[];
            videoRecorder.ondataavailable=e=>videoChunks.push(e.data);
            videoRecorder.onstop=()=>{
                const r=new FileReader(); r.onloadend=()=>socket.emit('send_msg',{sender:currentUser,target:currentTarget,msg:r.result.split(',')[1],msg_type:'video_note'});
                r.readAsDataURL(new Blob(videoChunks,{type:'audio/webm'}));
                s.getTracks().forEach(t=>t.stop());
            };
            videoRecorder.start(); isVideoRecording=true; b.classList.add('recording');
        } catch(e){ alert('Ошибка камеры/микрофона!'); }
    } else {
        videoRecorder.stop(); isVideoRecording=false; b.classList.remove('recording');
    }
}

if(socket) {
    socket.on('new_msg',d=>{
        if(d.sender!==currentUser){
            playNotificationSound(); triggerSystemNotification(d.sender,d.msg);
            if(d.target!==currentTarget&&d.sender!==currentTarget){
                const k=d.target.startsWith('#')?d.target:d.sender; unreadCounts[k]=(unreadCounts[k]||0)+1;
                socket.emit('get_channels'); socket.emit('get_users');
            }
        }
        if(d.target===currentTarget||d.sender===currentTarget||(d.target.startsWith('#')&&d.target===currentTarget)){
            appendMessage(d); socket.emit('mark_read',{user:currentUser,target:currentTarget});
        }
    });

    socket.on('messages_read',d=>{ if(d.target===currentUser||d.user===currentTarget) document.querySelectorAll('.msg-status').forEach(s=>s.innerText='✓✓'); });
}

function appendMessage(m){
    allLoadedMessages[m.id]=m; let div=document.getElementById('msg-elem-'+m.id);
    const box=document.getElementById('messages'), isMe=m.sender===currentUser;
    if(!div){
        div=document.createElement('div'); div.id='msg-elem-'+m.id;
        div.className='message '+(isMe?'msg-me':'msg-other'); div.setAttribute('tabindex','0');
        box.appendChild(div);
    }
    let rHtml=m.reply_text?'<div class="reply-preview-box">'+m.reply_text+'</div>':'', cHtml='<span>'+m.msg+'</span>';
    if(m.msg_type==='file'){
        try {
            const f=JSON.parse(m.msg);
            if(f.name.match(/\.(jpg|jpeg|png|gif|webp)$/i)){
                cHtml='<img src="data:image/jpeg;base64,'+f.data+'" style="max-width:180px;border-radius:8px;cursor:pointer;" onclick="openLightbox(this.src)">';
            } else {
                cHtml='<a href="data:application/octet-stream;base64,'+f.data+'" download="'+f.name+'" style="color:#fff;">📁 '+f.name+'</a>';
            }
        } catch(e){}
    } else if(m.msg_type==='voice'){
        cHtml='<audio controls playsinline src="data:audio/webm;base64,'+m.msg+'" style="height:32px;width:170px;"></audio>';
    } else if(m.msg_type==='video_note'){
        cHtml='<video class="video-note" controls playsinline src="data:video/webm;base64,'+m.msg+'" onclick="openVideoLightbox(this.src)"></video>';
    } else if(m.msg_type==='sticker'){
        cHtml='<img src="'+m.msg+'" class="sticker-img">';
    }
    let reactObj={}; try { reactObj=JSON.parse(m.reactions||'{}'); } catch(e){}
    let reactHtml=''; for(const[emoji,users] of Object.entries(reactObj)){
        if(users.length>0) reactHtml+='<span class="reaction-item" onclick="addReaction('+m.id+',\''+emoji+'\')">'+emoji+' '+users.length+'</span>';
    }
    const avUrl=userAvatarsMap[m.sender], avStyle=(avUrl&&avUrl.length>10)?'background-image:url("'+avUrl+'")':'background-color:'+getAvatarColor(m.sender);
    div.innerHTML='<div class="msg-actions"><button class="action-btn" onclick="setReplyMsg('+m.id+')">↩️</button><button class="action-btn" onclick="pinMsg('+m.id+')">📌</button><button class="action-btn" onclick="addReaction('+m.id+',\'👍\')">👍</button><button class="action-btn" onclick="addReaction('+m.id+',\'❤️\')">❤️</button>'+(isMe&&m.msg_type==='text'?'<button class="action-btn" onclick="startEditMsg('+m.id+')">✏️</button>':'')+((isMe||userRole==='admin')?'<button class="action-btn" onclick="deleteMessage('+m.id+')">🗑️</button>':'')+'</div>'+rHtml+'<div class="msg-header"><span class="msg-avatar" style="'+avStyle+'" onclick="openUserInfoModal(\''+m.sender+'\')"></span><span onclick="openUserInfoModal(\''+m.sender+'\')" style="cursor:pointer;">'+(isMe?'Вы':m.sender)+(m.ttl>0?' 💣('+m.ttl+'s)':'')+'</span></div>'+cHtml+(m.is_edited?'(изм.)':'')+'<div class="msg-status">'+(isMe?(m.is_read?'✓✓':'✓'):'')+'</div><div class="reactions-list">'+reactHtml+'</div>';
    div.onclick=e=>{ e.stopPropagation(); document.querySelectorAll('.message').forEach(x=>x.classList.remove('active-touch')); div.classList.add('active-touch'); };
    box.scrollTop=box.scrollHeight; if(m.ttl>0) setTimeout(()=>{ deleteMessage(m.id); }, m.ttl*1000);
}

function setReplyMsg(id){ const m=allLoadedMessages[id]; if(m){ activeReplyMsg=m; document.getElementById('reply-text').innerText='Ответ для '+m.sender+': '+m.msg; document.getElementById('reply-bar').style.display='flex'; } }
function addReaction(id,e){ socket.emit('add_reaction',{msg_id:id,emoji:e,user:currentUser}); }
if(socket) { socket.on('update_msg_reactions',d=>{ if(allLoadedMessages[d.msg_id]){ allLoadedMessages[d.msg_id].reactions=JSON.stringify(d.reactions); appendMessage(allLoadedMessages[d.msg_id]); } }); }
function deleteMessage(id){ socket.emit('delete_msg',{msg_id:id,user:currentUser}); }
if(socket) { socket.on('msg_deleted',d=>{ const e=document.getElementById('msg-elem-'+d.msg_id); if(e) e.remove(); }); }
function searchMessages(){ const q=document.getElementById('search-input').value.toLowerCase(); document.querySelectorAll('.message').forEach(d=>{ d.style.display=d.innerText.toLowerCase().includes(q)?'flex':'none'; }); }

async function startDirectCall(){
    if(currentTarget.startsWith('#')) return;
    document.getElementById('call-modal').style.display='flex';
    document.getElementById('call-user-name').innerText='@'+currentTarget;
    document.getElementById('call-status-text').innerText='Вызов...';
    document.getElementById('accept-call-btn').style.display='none';
    setAvatarElement(document.getElementById('call-user-avatar'),currentTarget);
    try {
        directCallStream=await navigator.mediaDevices.getUserMedia({audio:true});
        activeCallPeer=new RTCPeerConnection(iceServers);
        directCallStream.getTracks().forEach(t=>activeCallPeer.addTrack(t,directCallStream));
        activeCallPeer.onicecandidate=e=>{ if(e.candidate) socket.emit('call_signal',{target:currentTarget,candidate:e.candidate}); };
        activeCallPeer.ontrack=e=>{ document.getElementById('direct-call-audio').srcObject=e.streams[0]; };
        const o=await activeCallPeer.createOffer(); await activeCallPeer.setLocalDescription(o);
        socket.emit('call_start',{caller:currentUser,target:currentTarget,offer:o});
    } catch(e){ alert('Микрофон недоступен!'); endCall(); }
}

if(socket) {
    socket.on('incoming_call',d=>{
        incomingCallData=d; document.getElementById('call-modal').style.display='flex';
        document.getElementById('call-user-name').innerText='@'+d.caller;
        document.getElementById('call-status-text').innerText='Входящий звонок...';
        document.getElementById('accept-call-btn').style.display='inline-block';
        setAvatarElement(document.getElementById('call-user-avatar'),d.caller);
    });
}

async function acceptCall(){
    document.getElementById('accept-call-btn').style.display='none';
    document.getElementById('call-status-text').innerText='Разговор...';
    try {
        directCallStream=await navigator.mediaDevices.getUserMedia({audio:true});
        activeCallPeer=new RTCPeerConnection(iceServers);
        directCallStream.getTracks().forEach(t=>activeCallPeer.addTrack(t,directCallStream));
        activeCallPeer.onicecandidate=e=>{ if(e.candidate) socket.emit('call_signal',{target:incomingCallData.caller,candidate:e.candidate}); };
        activeCallPeer.ontrack=e=>{ document.getElementById('direct-call-audio').srcObject=e.streams[0]; };
        await activeCallPeer.setRemoteDescription(new RTCSessionDescription(incomingCallData.offer));
        const a=await activeCallPeer.createAnswer(); await activeCallPeer.setLocalDescription(a);
        socket.emit('call_accept',{target:incomingCallData.caller,answer:a});
    } catch(e){ alert('Ошибка соединения!'); endCall(); }
}

if(socket) {
    socket.on('call_accepted',async d=>{ document.getElementById('call-status-text').innerText='Разговор...'; if(activeCallPeer) await activeCallPeer.setRemoteDescription(new RTCSessionDescription(d.answer)); });
    socket.on('call_signal',async d=>{ if(activeCallPeer&&d.candidate) await activeCallPeer.addIceCandidate(new RTCIceCandidate(d.candidate)); });
    socket.on('call_ended',()=>{ endCallLocal(); });
}

function endCall(){ if(socket) socket.emit('call_end',{target:incomingCallData?incomingCallData.caller:currentTarget}); endCallLocal(); }
function endCallLocal(){
    if(directCallStream) directCallStream.getTracks().forEach(t=>t.stop());
    if(activeCallPeer) activeCallPeer.close();
    activeCallPeer=null; directCallStream=null; incomingCallData=null;
    document.getElementById('call-modal').style.display='none';
}

function openMafiaModal(){ closeProfileModal(); document.getElementById('mafia-modal').style.display='flex'; backToMafiaRooms(); }
function closeMafiaModal(){ document.getElementById('mafia-modal').style.display='none'; }
function backToMafiaRooms(){ if(activeRoomId) leaveActiveRoom(); activeRoomId=null; document.getElementById('mafia-rooms-screen').style.display='block'; document.getElementById('mafia-gameplay-screen').style.display='none'; if(socket) socket.emit('mafia_get_rooms'); }
function enterMafiaRoom(r){ activeRoomId=r; document.getElementById('mafia-rooms-screen').style.display='none'; document.getElementById('mafia-gameplay-screen').style.display='block'; if(socket) socket.emit('mafia_get_room_state',{room_id:activeRoomId}); }
function joinActiveRoom(){ if(socket) socket.emit('mafia_join',{room_id:activeRoomId,user:currentUser}); }
function leaveActiveRoom(){ stopMicMeter(); if(socket) socket.emit('mafia_leave',{room_id:activeRoomId,user:currentUser}); }
function startActiveRoomGame(){ if(socket) socket.emit('mafia_start',{room_id:activeRoomId}); }
function toggleRoleShow(){ showRoleFlag=!showRoleFlag; document.getElementById('my-role-text').innerText=showRoleFlag?(mySecretRole||'Нет роли'):'Скрыта'; }

async function toggleMic(){
    isMicOn=!isMicOn; const b=document.getElementById('mic-toggle-btn');
    if(isMicOn){
        try {
            mediaStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
            b.innerText='🎙️ Микрофон ВКЛ'; b.style.background='#388e3c'; startMicMeter(mediaStream);
            if(socket) socket.emit('webrtc_join',{room_id:activeRoomId,user:currentUser});
        } catch(e){ alert('Ошибка!'); isMicOn=false; }
    } else {
        stopMicMeter(); b.innerText='🔇 Микрофон ВЫКЛ'; b.style.background='#c62828';
    }
}

function startMicMeter(s){
    audioContext=new(window.AudioContext||window.webkitAudioContext)();
    if(audioContext.state==='suspended') audioContext.resume();
    analyser=audioContext.createAnalyser(); audioContext.createMediaStreamSource(s).connect(analyser); analyser.fftSize=64;
    const d=new Uint8Array(analyser.frequencyBinCount), mb=document.getElementById('meter-box-'+currentUser), bar=document.getElementById('meter-bar-'+currentUser);
    if(mb) mb.style.display='block';
    function upd(){
        analyser.getByteFrequencyData(d); let sum=0; for(let i=0;i<d.length;i++) sum+=d[i];
        if(bar) bar.style.width=Math.min(100,Math.round(((sum/d.length)/128)*100))+'%';
        micAnimFrame=requestAnimationFrame(upd);
    }
    upd();
}

function createPeer(s){
    if(peerConnections[s]) return peerConnections[s];
    const pc=new RTCPeerConnection(iceServers); peerConnections[s]=pc;
    if(mediaStream) mediaStream.getTracks().forEach(t=>pc.addTrack(t,mediaStream));
    pc.onicecandidate=e=>{ if(e.candidate&&socket) socket.emit('webrtc_signal',{target_sid:s,candidate:e.candidate}); };
    pc.ontrack=e=>{
        let a=document.getElementById('audio-'+s);
        if(!a){ a=document.createElement('audio'); a.id='audio-'+s; a.autoplay=true; a.playsInline=true; document.getElementById('audio-peers-container').appendChild(a); }
        a.srcObject=e.streams[0]; a.play().catch(e=>{});
    };
    return pc;
}

if(socket) {
    socket.on('webrtc_user_joined',async d=>{ if(d.sid===socket.id) return; const pc=createPeer(d.sid), o=await pc.createOffer(); await pc.setLocalDescription(o); socket.emit('webrtc_signal',{target_sid:d.sid,offer:o}); });
    socket.on('webrtc_signal',async d=>{
        const s=d.sender_sid; if(s===socket.id) return; const pc=createPeer(s);
        if(d.offer){ await pc.setRemoteDescription(new RTCSessionDescription(d.offer)); const a=await pc.createAnswer(); await pc.setLocalDescription(a); socket.emit('webrtc_signal',{target_sid:s,answer:a}); }
        else if(d.answer) await pc.setRemoteDescription(new RTCSessionDescription(d.answer));
        else if(d.candidate) await pc.addIceCandidate(new RTCIceCandidate(d.candidate));
    });
}

function stopMicMeter(){
    if(micAnimFrame) cancelAnimationFrame(micAnimFrame);
    if(mediaStream) mediaStream.getTracks().forEach(t=>t.stop());
    if(audioContext) audioContext.close();
    for(let s in peerConnections) peerConnections[s].close();
    peerConnections={}; document.getElementById('audio-peers-container').innerHTML=''; isMicOn=false;
    const mb=document.getElementById('meter-box-'+currentUser); if(mb) mb.style.display='none';
}

if(socket) {
    socket.on('mafia_rooms_list',d=>{
        const c=document.getElementById('mafia-rooms-container'); c.innerHTML='';
        for(const[r,info] of Object.entries(d.rooms)){
            const it=document.createElement('div'); it.className='room-item';
            it.innerHTML='<div><div class="room-title">'+info.name+'</div><div class="room-info">Игроков: '+info.count+'/6 | '+info.state.toUpperCase()+'</div></div><button style="width:auto;padding:6px 14px;" onclick="enterMafiaRoom(\''+r+'\')">Войти</button>';
            c.appendChild(it);
        }
    });

    socket.on('mafia_room_update',d=>{
        if(d.room_id!==activeRoomId) return;
        document.getElementById('mafia-modal-title').innerText='🕵️ '+d.name;
        document.getElementById('room-phase-badge').innerText=d.state.toUpperCase();
        const tb=document.getElementById('mafia-timer-badge');
        if(d.timer_end>0){ tb.innerText='⏱ '+Math.max(0,d.timer_end-Math.floor(Date.now()/1000))+'s'; tb.style.display='inline-block'; }
        else tb.style.display='none';

        if(d.players[currentUser]){
            mySecretRole=d.players[currentUser].role;
            document.getElementById('join-room-btn').style.display='none';
            document.getElementById('leave-room-btn').style.display='inline-block';
        } else {
            document.getElementById('join-room-btn').style.display='inline-block';
            document.getElementById('leave-room-btn').style.display='none';
        }

        const tbl=document.getElementById('poker-table-slots'); tbl.innerHTML='';
        const pBySlot={}; for(const[u,p] of Object.entries(d.players)) pBySlot[p.slot]={name:u,...p};

        for(let s=0;s<6;s++){
            const div=document.createElement('div');
            if(pBySlot[s]){
                const p=pBySlot[s]; div.className='table-slot occupied'; let act='';
                if(p.alive&&p.name!==currentUser){
                    if(d.state==='night'&&mySecretRole==='mafia') act='<button class="act-btn" onclick="socket.emit(\'mafia_action\',{room_id:\''+activeRoomId+'\',user:\''+currentUser+'\',target:\''+p.name+'\',action:\'kill\'})">Убить</button>';
                    if(d.state==='day') act='<button class="act-btn" style="background:#f57c00;" onclick="socket.emit(\'mafia_action\',{room_id:\''+activeRoomId+'\',user:\''+currentUser+'\',target:\''+p.name+'\',action:\'vote\'})">Голос</button>';
                }
                const av=document.createElement('div'); av.className='slot-avatar'; setAvatarElement(av,p.name);
                div.appendChild(av);
                div.innerHTML+='<div class="slot-name">'+p.name+'</div><div class="audio-meter-box" id="meter-box-'+p.name+'"><div class="audio-meter-bar" id="meter-bar-'+p.name+'"></div></div><div class="slot-status">'+(p.alive?'🟢 Жив':'💀 Убит')+'</div>'+act;
            } else {
                div.className='table-slot'; div.innerHTML='<div class="slot-avatar" style="background:#333;color:#666;">+</div><div class="slot-name" style="color:#777;">Свободно</div>';
            }
            tbl.appendChild(div);
        }
        const lb=document.getElementById('room-logs-box'); lb.innerHTML=d.logs.join('<br>'); lb.scrollTop=lb.scrollHeight;
    });
}

function openProfileModal(){ document.getElementById('profile-modal').style.display='flex'; }
function closeProfileModal(){ document.getElementById('profile-modal').style.display='none'; }

if(socket) {
    socket.on('disconnect',r=>{ if(r==='io server disconnect'||r==='transport close') socket.connect(); });
}

document.addEventListener('visibilitychange',()=>{
    if(document.visibilityState==='visible'){
        if(socket && !socket.connected) socket.connect();
        if(currentUser && socket){
            socket.emit('get_users');
            if(currentTarget) socket.emit('load_history',{user:currentUser,target:currentTarget});
        }
    }
});
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/manifest.json')
def serve_manifest():
    return {
        "name": "Спецзвязочок Ultra",
        "short_name": "Спецзвязок",
        "description": "Защищенный мессенджер с поддержкой звонков и игр",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#121212",
        "theme_color": "#1F6AA5",
        "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/732/732200.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]
    }, 200, {'Content-Type': 'application/json'}

@app.route('/sw.js')
def serve_sw():
    return """
    self.addEventListener('install', (e) => self.skipWaiting());
    self.addEventListener('activate', (e) => e.waitUntil(clients.claim()));
    self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)));
    self.addEventListener('notificationclick', function(event) {
        event.notification.close();
        event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
            for (let c of list) { if ('focus' in c) return c.focus(); }
            if (clients.openWindow) return clients.openWindow('/');
        }));
    });
    """, 200, {'Content-Type': 'application/javascript'}

@socketio.on('connect')
def on_connect(): pass

@socketio.on('disconnect')
def on_disconnect():
    for uname, sid in list(online_users.items()):
        if sid == request.sid:
            del online_users[uname]
            conn, db_type = get_db()
            cur = conn.cursor()
            now = int(time.time())
            if db_type == 'postgres': cur.execute("UPDATE users SET last_seen = %s WHERE username = %s", (now, uname))
            else: cur.execute("UPDATE users SET last_seen = ? WHERE username = ?", (now, uname))
            conn.commit()
            conn.close()
            on_get_users()
            break

@socketio.on('login')
def on_login(data):
    username, password = data.get('username', '').strip(), data.get('password', '').strip()
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres': cur.execute("SELECT password_hash, salt, role, avatar FROM users WHERE username = %s", (username,))
    else: cur.execute("SELECT password_hash, salt, role, avatar FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if row and hash_password(password, row[1])[0] == row[0]:
        conn.close()
        online_users[username] = request.sid
        join_room(username); join_room('#general')
        emit('auth_response', {'status': 'ok', 'username': username, 'role': row[2], 'avatar': row[3]})
        on_get_users()
    else:
        conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Неверный логин или пароль'})

@socketio.on('register')
def on_register(data):
    username, password = data.get('username', '').strip(), data.get('password', '').strip()
    if not username or not password: return
    pwd_hash, salt = hash_password(password)
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        role = 'admin' if username == 'totr' else 'user'
        if db_type == 'postgres': cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (%s, %s, %s, %s)", (username, pwd_hash, salt, role))
        else: cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)", (username, pwd_hash, salt, role))
        conn.commit()
        emit('auth_response', {'status': 'error', 'message': 'Успешно! Теперь войдите.'})
    except Exception: emit('auth_response', {'status': 'error', 'message': 'Логин занят'})
    finally: conn.close()

@socketio.on('call_start')
def on_call_start(data): emit('incoming_call', data, room=data['target'])
@socketio.on('call_accept')
def on_call_accept(data): emit('call_accepted', data, room=data['target'])
@socketio.on('call_signal')
def on_call_signal(data): emit('call_signal', data, room=data['target'])
@socketio.on('call_end')
def on_call_end(data): emit('call_ended', data, room=data['target'])

@socketio.on('create_channel')
def on_create_channel(data):
    ch_name, creator = data['name'].strip(), data['creator']
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        if db_type == 'postgres': cur.execute("INSERT INTO channels (name, creator) VALUES (%s, %s)", (ch_name, creator))
        else: cur.execute("INSERT INTO channels (name, creator) VALUES (?, ?)", (ch_name, creator))
        conn.commit(); join_room(ch_name)
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
    admin, target_user, new_role = data['admin'], data['target_user'], data['role']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "SELECT role FROM users WHERE username = %s" if db_type == 'postgres' else "SELECT role FROM users WHERE username = ?"
    cur.execute(q, (admin,))
    row = cur.fetchone()
    if (row and row[0] == 'admin') or admin == 'totr':
        u_q = "UPDATE users SET role = %s WHERE username = %s" if db_type == 'postgres' else "UPDATE users SET role = ? WHERE username = ?"
        cur.execute(u_q, (new_role, target_user))
        conn.commit()
    conn.close()
    on_admin_get_users()

@socketio.on('admin_delete_user')
def on_admin_delete_user(data):
    admin, target_user = data['admin'], data['target_user']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "SELECT role FROM users WHERE username = %s" if db_type == 'postgres' else "SELECT role FROM users WHERE username = ?"
    cur.execute(q, (admin,))
    row = cur.fetchone()
    if (row and row[0] == 'admin') or admin == 'totr':
        d_q = "DELETE FROM users WHERE username = %s" if db_type == 'postgres' else "DELETE FROM users WHERE username = ?"
        cur.execute(d_q, (target_user,))
        conn.commit()
    conn.close()
    on_admin_get_users()

@socketio.on('update_avatar')
def on_update_avatar(data):
    user, avatar = data['user'], data['avatar']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "UPDATE users SET avatar = %s WHERE username = %s" if db_type == 'postgres' else "UPDATE users SET avatar = ? WHERE username = ?"
    cur.execute(q, (avatar, user))
    conn.commit(); conn.close()
    emit('avatar_updated', {'user': user, 'avatar': avatar}, broadcast=True)

@socketio.on('change_password')
def on_change_password(data):
    user, old_p, new_p = data['user'], data['old_pass'], data['new_pass']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "SELECT password_hash, salt FROM users WHERE username = %s" if db_type == 'postgres' else "SELECT password_hash, salt FROM users WHERE username = ?"
    cur.execute(q, (user,))
    row = cur.fetchone()
    if row and hash_password(old_p, row[1])[0] == row[0]:
        new_hash, new_salt = hash_password(new_p)
        u_q = "UPDATE users SET password_hash = %s, salt = %s WHERE username = %s" if db_type == 'postgres' else "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?"
        cur.execute(u_q, (new_hash, new_salt, user))
        conn.commit(); conn.close()
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
    target, msg = data['target'], data['msg']
    if target.startswith('#'):
        conn, db_type = get_db()
        cur = conn.cursor()
        q = "UPDATE channels SET pinned_msg = %s WHERE name = %s" if db_type == 'postgres' else "UPDATE channels SET pinned_msg = ? WHERE name = ?"
        cur.execute(q, (msg, target))
        conn.commit(); conn.close()
    emit('update_pinned', {'target': target, 'msg': msg}, broadcast=True)

@socketio.on('mark_read')
def on_mark_read(data):
    user, target = data['user'], data['target']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "UPDATE messages SET is_read = 1 WHERE target = %s AND sender = %s" if db_type == 'postgres' else "UPDATE messages SET is_read = 1 WHERE target = ? AND sender = ?"
    cur.execute(q, (user, target))
    conn.commit(); conn.close()
    emit('messages_read', {'user': user, 'target': target}, room=target)

@socketio.on('load_history')
def on_load_history(data):
    user, target = data['user'], data['target']
    conn, db_type = get_db()
    cur = conn.cursor()
    pinned_msg = ""
    if target.startswith('#'):
        join_room(target)
        cur.execute("SELECT pinned_msg FROM channels WHERE name = %s" if db_type == 'postgres' else "SELECT pinned_msg FROM channels WHERE name = ?", (target,))
        p_row = cur.fetchone()
        if p_row: pinned_msg = p_row[0]
        q = "SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited FROM messages WHERE target = %s ORDER BY id ASC LIMIT 200" if db_type == 'postgres' else "SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited FROM messages WHERE target = ? ORDER BY id ASC LIMIT 200"
        cur.execute(q, (target,))
    else:
        q = "SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited FROM messages WHERE (sender = %s AND target = %s) OR (sender = %s AND target = %s) ORDER BY id ASC LIMIT 200" if db_type == 'postgres' else "SELECT id, sender, target, message, msg_type, reply_to, reply_text, reactions, ttl, is_read, is_edited FROM messages WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) ORDER BY id ASC LIMIT 200"
        cur.execute(q, (user, target, target, user))
    rows = cur.fetchall()
    conn.close()
    msgs = [{'id': r[0], 'sender': r[1], 'target': r[2], 'msg': r[3], 'msg_type': r[4], 'reply_to': r[5], 'reply_text': r[6], 'reactions': r[7], 'ttl': r[8], 'is_read': r[9], 'is_edited': r[10]} for r in rows]
    emit('history_loaded', {'messages': msgs, 'pinned_msg': pinned_msg})

@socketio.on('send_msg')
def on_send_msg(data):
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("INSERT INTO messages (sender, target, message, msg_type, reply_to, reply_text, ttl) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", (data['sender'], data['target'], data['msg'], data.get('msg_type', 'text'), data.get('reply_to'), data.get('reply_text', ''), data.get('ttl', 0)))
        msg_id = cur.fetchone()[0]
    else:
        cur.execute("INSERT INTO messages (sender, target, message, msg_type, reply_to, reply_text, ttl) VALUES (?, ?, ?, ?, ?, ?, ?)", (data['sender'], data['target'], data['msg'], data.get('msg_type', 'text'), data.get('reply_to'), data.get('reply_text', ''), data.get('ttl', 0)))
        msg_id = cur.lastrowid
    conn.commit(); conn.close()
    data.update({'id': msg_id, 'is_read': 0, 'is_edited': 0})
    target = data['target']
    if target.startswith('#'): emit('new_msg', data, room=target)
    else:
        emit('new_msg', data, room=target)
        if data['sender'] != target: emit('new_msg', data, room=data['sender'])

@socketio.on('edit_msg')
def on_edit_msg(data):
    msg_id, new_text = data['msg_id'], data['new_text']
    conn, db_type = get_db()
    cur = conn.cursor()
    q = "UPDATE messages SET message = %s, is_edited = 1 WHERE id = %s" if db_type == 'postgres' else "UPDATE messages SET message = ?, is_edited = 1 WHERE id = ?"
    cur.execute(q, (new_text, msg_id))
    conn.commit(); conn.close()
    emit('msg_edited', {'msg_id': msg_id, 'new_text': new_text}, broadcast=True)

@socketio.on('add_reaction')
def on_add_reaction(data):
    msg_id, emoji, user = data['msg_id'], data['emoji'], data['user']
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT reactions FROM messages WHERE id = %s" if db_type == 'postgres' else "SELECT reactions FROM messages WHERE id = ?", (msg_id,))
    row = cur.fetchone()
    if row:
        reactions = json.loads(row[0] or '{}')
        if emoji not in reactions: reactions[emoji] = []
        if user in reactions[emoji]: reactions[emoji].remove(user)
        else: reactions[emoji].append(user)
        q = "UPDATE messages SET reactions = %s WHERE id = %s" if db_type == 'postgres' else "UPDATE messages SET reactions = ? WHERE id = ?"
        cur.execute(q, (json.dumps(reactions), msg_id))
        conn.commit()
    conn.close()
    emit('update_msg_reactions', {'msg_id': msg_id, 'reactions': reactions}, broadcast=True)

@socketio.on('delete_msg')
def on_delete_msg(data):
    msg_id = data['msg_id']
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id = %s" if db_type == 'postgres' else "DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit(); conn.close()
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
    emit('mafia_room_update', {'room_id': room.room_id, 'name': room.name, 'state': room.state, 'players': room.players, 'logs': room.logs, 'timer_end': room.timer_end}, broadcast=True)

@socketio.on('mafia_get_rooms')
def on_mafia_get_rooms():
    emit('mafia_rooms_list', {'rooms': {rid: {'name': r.name, 'state': r.state, 'count': len(r.players)} for rid, r in mafia_rooms.items()}})

@socketio.on('mafia_get_room_state')
def on_mafia_get_room_state(data): broadcast_room_state(data['room_id'])

@socketio.on('mafia_join')
def on_mafia_join(data):
    rid, room = data['room_id'], mafia_rooms[data['room_id']]
    if room.add_player(data['user']):
        room.logs.append(f"Игрок **{data['user']}** занял место.")
        broadcast_room_state(rid)

@socketio.on('mafia_leave')
def on_mafia_leave(data):
    rid, room = data['room_id'], mafia_rooms[data['room_id']]
    if room.remove_player(data['user']):
        room.logs.append(f"Игрок **{data['user']}** покинул место.")
        broadcast_room_state(rid)

@socketio.on('mafia_start')
def on_mafia_start(data):
    rid, room = data['room_id'], mafia_rooms[data['room_id']]
    ok, msg = room.start_game()
    if ok: broadcast_room_state(rid)

@socketio.on('mafia_action')
def on_mafia_action(data):
    rid, room, user, target, act = data['room_id'], mafia_rooms[data['room_id']], data['user'], data['target'], data['action']
    if room.state == 'night':
        if act == 'kill' and room.players[user]['role'] == 'mafia': room.night_actions['mafia'][user] = target
        alive_mafia = [u for u, p in room.players.items() if p['alive'] and p['role'] == 'mafia']
        if len(room.night_actions['mafia']) >= len(alive_mafia):
            room.process_night(); broadcast_room_state(rid)
    elif room.state == 'day':
        if act == 'vote':
            room.day_votes[user] = target
            alive_count = sum(1 for p in room.players.values() if p['alive'])
            if len(room.day_votes) >= alive_count:
                room.process_day(); broadcast_room_state(rid)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
