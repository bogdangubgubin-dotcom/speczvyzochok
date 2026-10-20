import os
import sqlite3
import hashlib
import time
import json
import random
import psutil
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'speczvyazok_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "messenger.db")

ADMIN_PASSWORD_HASH = hashlib.sha256('1234'.encode('utf-8')).hexdigest()

def get_db():
    return sqlite3.connect(DB_PATH, timeout=10)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            last_seen INTEGER DEFAULT 0
        )
    """)
    
    cur.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cur.fetchall()]
    if 'role' not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    if 'last_seen' not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER DEFAULT 0")
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            creator TEXT NOT NULL,
            members TEXT DEFAULT ''
        )
    """)
    
    cur.execute("INSERT OR IGNORE INTO channels (name, creator, members) VALUES ('#general', 'system', '')")

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
            is_read INTEGER DEFAULT 0,
            is_edited INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cur.execute("PRAGMA table_info(messages)")
    msg_cols = [col[1] for col in cur.fetchall()]
    if 'reply_to' not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN reply_to INTEGER DEFAULT NULL")
    if 'reply_text' not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN reply_text TEXT DEFAULT ''")
    if 'reactions' not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN reactions TEXT DEFAULT '{}'")
    if 'is_read' not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0")
    if 'is_edited' not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN is_edited INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            username TEXT PRIMARY KEY,
            until INTEGER
        )
    """)
    
    cur.execute("SELECT username FROM users WHERE username = 'totr'")
    if cur.fetchone():
        cur.execute("UPDATE users SET role = 'admin' WHERE username = 'totr'")
        
    conn.commit()
    conn.close()

init_db()

def hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

# --- ДВИЖОК 5 КОМНАТ МАФИИ ---
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
            return True
        return False

    def start_game(self):
        plist = list(self.players.keys())
        if len(plist) < 3:
            return False, "Нужно минимум 3 игрока!"

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
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        self.day_votes = {}
        self.logs = ["Игра началась! Наступила ночь."]
        return True, "Игра запущена!"

    def check_win(self):
        alive_mafia = sum(1 for p in self.players.values() if p['alive'] and p['role'] == 'mafia')
        alive_others = sum(1 for p in self.players.values() if p['alive'] and p['role'] != 'mafia')

        if alive_mafia == 0:
            self.state = 'ended'
            return "Мирные жители победили!"
        if alive_mafia >= alive_others:
            self.state = 'ended'
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
        else:
            msg += "Ночью никто не погиб!"

        self.logs.append(msg)
        win = self.check_win()
        if win:
            self.logs.append(f"🎉 **{win}**")
        else:
            self.state = 'day'
            for u, p in self.players.items():
                if p['alive']: p['mic'] = True

        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        return msg

    def process_day(self):
        if not self.day_votes:
            self.logs.append("Днем никто не был исключен.")
        else:
            targets = list(self.day_votes.values())
            exiled = max(set(targets), key=targets.count)
            self.players[exiled]['alive'] = False
            self.players[exiled]['mic'] = False
            self.logs.append(f"Город выгнал игрока **{exiled}** ({self.players[exiled]['role']}).")

        win = self.check_win()
        if win:
            self.logs.append(f"🎉 **{win}**")
        else:
            self.state = 'night'
            self.logs.append("Наступает ночь... Город засыпает.")
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
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        html, body { width: 100vw; height: 100dvh; overflow: hidden; background: #121212; color: #fff; position: fixed; top: 0; left: 0; }
        
        #auth-screen { display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100dvh; padding: 20px; }
        .auth-box { background: #1e1e1e; padding: 25px; border-radius: 16px; width: 100%; max-width: 340px; text-align: center; border: 1px solid #2d2d2d; }
        .auth-box h2 { margin-bottom: 15px; color: #3B8ED0; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin: 8px 0; border-radius: 10px; border: 1px solid #333; background: #2a2a2a; color: #fff; outline: none; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none; background: #1F6AA5; color: #fff; font-weight: bold; cursor: pointer; font-size: 15px; }
        
        #app-screen { display: none; height: 100dvh; width: 100vw; position: relative; overflow: hidden; }
        
        /* САЙДБАР С ЛИНИЕЙ РАЗДЕЛИТЕЛЯ */
        #sidebar { width: 280px; background: #1a1a1a; border-right: 2px solid #2d2d2d; display: flex; flex-direction: column; flex-shrink: 0; z-index: 10; transition: transform 0.2s ease-in-out; }
        .sidebar-header { padding: 12px 15px; background: #1e1e1e; border-bottom: 1px solid #2d2d2d; font-weight: bold; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
        .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
        .section-title { font-size: 11px; text-transform: uppercase; color: #888; margin: 12px 8px 6px 8px; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        
        .chat-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; font-size: 14px; background: transparent; transition: background 0.2s; }
        .chat-item:hover, .chat-item.active { background: #2a2a2a; }
        
        .avatar { width: 34px; height: 34px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; font-size: 14px; flex-shrink: 0; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-left: auto; }
        .online { background: #4caf50; } .offline { background: #757575; }

        /* ГЛАВНАЯ ОБЛАСТЬ ЧАТА */
        #chat-main { flex: 1; display: flex; flex-direction: column; background: #121212; min-width: 0; height: 100dvh; position: relative; }
        header { height: 56px; background: #1e1e1e; padding: 0 15px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2d2d2d; flex-shrink: 0; width: 100%; }
        .header-left { display: flex; align-items: center; gap: 10px; }
        .header-title-box { display: flex; flex-direction: column; }
        .header-status { font-size: 11px; color: #888; }
        .toggle-btn { width: 36px; height: 36px; background: #2a2a2a; border: 1px solid #333; font-size: 16px; border-radius: 8px; color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }

        .search-bar { padding: 6px 12px; background: #181818; border-bottom: 1px solid #2d2d2d; display: flex; gap: 8px; }
        .search-bar input { margin: 0; padding: 6px 10px; font-size: 13px; height: 32px; border-radius: 6px; }

        .chat-container { flex: 1; overflow-y: auto; padding: 15px; padding-bottom: 75px; display: flex; flex-direction: column; gap: 10px; -webkit-overflow-scrolling: touch; }
        .message { max-width: 80%; padding: 8px 12px; border-radius: 14px; font-size: 15px; position: relative; display: flex; flex-direction: column; gap: 4px; word-wrap: break-word; }
        .msg-me { background: #1F6AA5; align-self: flex-end; border-bottom-right-radius: 2px; }
        .msg-other { background: #2a2a2a; align-self: flex-start; border-bottom-left-radius: 2px; }
        .msg-sender { font-size: 11px; font-weight: bold; color: #82b1ff; }
        .msg-me .msg-sender { color: #e0e0e0; text-align: right; }

        #reply-bar { display: none; background: #222; padding: 6px 12px; border-top: 1px solid #333; justify-content: space-between; align-items: center; font-size: 13px; color: #3B8ED0; }
        
        footer { position: absolute; bottom: 0; left: 0; right: 0; height: 60px; background: #1e1e1e; padding: 8px 10px; display: flex; gap: 8px; border-top: 1px solid #2d2d2d; align-items: center; z-index: 99; }
        footer input[type="text"] { flex: 1; height: 42px; background: #2a2a2a; color: #fff; border: 1px solid #333; border-radius: 10px; padding: 0 12px; font-size: 16px; outline: none; }
        footer button { width: auto; padding: 0 16px; height: 42px; margin: 0; }
        .file-btn, .voice-btn { background: #333; border: 1px solid #444; border-radius: 10px; padding: 0 12px; height: 42px; display: flex; align-items: center; justify-content: center; cursor: pointer; color: #fff; flex-shrink: 0; }
        .recording { background: #c62828 !important; }

        /* МОДАЛЬНЫЕ ОКНА */
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; padding: 15px; }
        .modal-box { background: #1e1e1e; border: 1px solid #333; border-radius: 12px; width: 100%; max-width: 480px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
        .modal-header { padding: 15px; background: #252525; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
        .modal-body { padding: 15px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }

        .room-item { background: #2a2a2a; border: 1px solid #333; border-radius: 10px; padding: 12px; display: flex; justify-content: space-between; align-items: center; }
        .room-title { font-weight: bold; font-size: 15px; }
        .room-info { font-size: 12px; color: #aaa; }
        
        /* КРУГЛЫЙ СТОЛ И ИНДИКАТОР ГРОМКОСТИ МИКРОФОНА */
        .poker-table { background: #1b4332; border: 6px solid #2d6a4f; border-radius: 120px; padding: 15px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; justify-items: center; align-items: center; margin: 10px 0; }
        .table-slot { width: 90px; height: 110px; background: rgba(0,0,0,0.4); border: 2px dashed #40916c; border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 6px; text-align: center; position: relative; }
        .table-slot.occupied { border-style: solid; border-color: #52b788; background: rgba(0,0,0,0.7); }
        .slot-avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 16px; margin-bottom: 2px; }
        .slot-name { font-size: 11px; font-weight: bold; color: #fff; width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .slot-status { font-size: 10px; color: #aaa; }

        /* МИНИАТЮРНЫЙ ИНДИКАТОР ГРОМКОСТИ (3px) */
        .audio-meter-box { width: 48px; height: 3px; background: #333; border-radius: 2px; overflow: hidden; margin: 2px 0; display: none; }
        .audio-meter-bar { height: 100%; width: 0%; background: #4caf50; transition: width 0.04s ease; }

        .logout-btn { background: transparent; border: 1px solid #555; color: #aaa; padding: 4px 8px; font-size: 12px; border-radius: 6px; cursor: pointer; width: auto; flex-shrink: 0; margin: 0; }

        @media (max-width: 600px) {
            #sidebar { position: absolute; left: 0; top: 0; bottom: 0; width: 80%; max-width: 280px; }
            #sidebar.hidden { transform: translateX(-100%); }
            .poker-table { grid-template-columns: repeat(2, 1fr); border-radius: 30px; }
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
        <div id="sidebar">
            <div class="sidebar-header">
                <div style="display:flex; align-items:center; gap:8px; cursor:pointer; overflow:hidden;" onclick="openProfileModal()">
                    <div id="my-avatar" class="avatar">?</div>
                    <span id="user-display" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:14px;">...</span>
                </div>
                <button class="logout-btn" onclick="logout()">Выйти</button>
            </div>
            <div class="chat-list">
                <div class="section-title">Каналы</div>
                <div id="channels-list"></div>
                <div class="section-title">Личные чаты</div>
                <div id="users-list"></div>
            </div>
        </div>

        <div id="chat-main">
            <header>
                <div class="header-left">
                    <button class="toggle-btn" onclick="toggleSidebar()">☰</button>
                    <div class="header-title-box">
                        <h3 id="chat-title">#general</h3>
                        <span id="chat-status" class="header-status"></span>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <span style="font-size:11px; color:#4caf50;">🔒 E2EE</span>
                </div>
            </header>

            <div class="search-bar">
                <input type="text" id="search-input" placeholder="🔍 Поиск по сообщениям..." oninput="searchMessages()">
            </div>

            <div class="chat-container" id="messages"></div>

            <div id="reply-bar">
                <span id="reply-text">Ответ...</span>
                <button onclick="cancelReply()" style="width:auto; padding:2px 8px; background:transparent; border:none; color:#aaa; cursor:pointer;">✕</button>
            </div>

            <footer>
                <label class="file-btn" title="Прикрепить файл">
                    📎
                    <input type="file" id="file-input" style="display:none;" onchange="sendFile()">
                </label>
                <button id="voice-record-btn" class="voice-btn" onclick="toggleVoiceRecord()" title="Голосовое сообщение">🎤</button>
                <input type="text" id="msg-input" placeholder="Сообщение..." onkeypress="if(event.key==='Enter') sendMsg()">
                <button onclick="sendMsg()">></button>
            </footer>
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
                <button onclick="openMafiaModal()" style="background:#8e24aa; margin-bottom:15px; padding:12px;">🕵️ Игра «Мафия»</button>
                <h4>Смена пароля</h4>
                <input type="password" id="old-pass" placeholder="Старый пароль">
                <input type="password" id="new-pass" placeholder="Новый пароль">
                <button onclick="changePassword()">Сохранить пароль</button>
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
                        <button onclick="backToMafiaRooms()" style="width:auto; padding:6px 12px; background:#444; font-size:12px;">← Назад к комнатам</button>
                        <span id="room-phase-badge" style="background:#3B8ED0; padding:4px 8px; border-radius:6px; font-size:12px; font-weight:bold;">Лобби</span>
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

    <script>
        const socket = io();
        let currentUser = "";
        let currentTarget = "#general";
        let activeRoomId = null;
        let showRoleFlag = false;
        let mySecretRole = null;
        
        let isMicOn = false;
        let mediaStream = null;
        let audioContext = null;
        let analyser = null;
        let micAnimFrame = null;

        let mediaRecorder = null;
        let audioChunks = [];
        let isRecording = false;

        function getAvatarColor(name) {
            let hash = 0;
            for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
            return '#' + ("00000" + (hash & 0x00FFFFFF).toString(16).toUpperCase()).slice(-6);
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
                localStorage.setItem('pychat_user', currentUser);
                localStorage.setItem('pychat_pass', document.getElementById('password').value || localStorage.getItem('pychat_pass'));
                document.getElementById('auth-screen').style.display = 'none';
                document.getElementById('app-screen').style.display = 'flex';
                document.getElementById('user-display').innerText = currentUser;
                
                const myAv = document.getElementById('my-avatar');
                myAv.innerText = currentUser.charAt(0).toUpperCase();
                myAv.style.background = getAvatarColor(currentUser);

                socket.emit('get_channels');
                socket.emit('get_users');
            } else {
                document.getElementById('status').innerText = data.message;
            }
        });

        function toggleSidebar() { document.getElementById('sidebar').classList.toggle('hidden'); }

        /* ОТРИСОВКА КАНАЛОВ И ПОЛЬЗОВАТЕЛЕЙ В САЙДБАРЕ */
        socket.on('channels_list', (data) => {
            const list = document.getElementById('channels-list');
            list.innerHTML = '';
            data.channels.forEach(ch => {
                const div = document.createElement('div');
                div.className = 'chat-item' + (ch === currentTarget ? ' active' : '');
                div.innerText = ch;
                div.onclick = () => {
                    currentTarget = ch;
                    document.getElementById('chat-title').innerText = ch;
                    document.querySelectorAll('.chat-item').forEach(i => i.classList.remove('active'));
                    div.classList.add('active');
                    document.getElementById('messages').innerHTML = '';
                };
                list.appendChild(div);
            });
        });

        socket.on('users_list', (data) => {
            const list = document.getElementById('users-list');
            list.innerHTML = '';
            data.users.forEach(u => {
                if (u.username === currentUser) return;
                const div = document.createElement('div');
                div.className = 'chat-item' + (u.username === currentTarget ? ' active' : '');
                div.innerHTML = `
                    <div class="avatar" style="background:${getAvatarColor(u.username)}">${u.username.charAt(0).toUpperCase()}</div>
                    <span>${u.username}</span>
                    <span class="status-dot ${u.online ? 'online' : 'offline'}"></span>
                `;
                div.onclick = () => {
                    currentTarget = u.username;
                    document.getElementById('chat-title').innerText = '@' + u.username;
                    document.querySelectorAll('.chat-item').forEach(i => i.classList.remove('active'));
                    div.classList.add('active');
                    document.getElementById('messages').innerHTML = '';
                };
                list.appendChild(div);
            });
        });

        function sendMsg() {
            const txt = document.getElementById('msg-input').value.trim();
            if (!txt) return;
            socket.emit('send_msg', { sender: currentUser, target: currentTarget, msg: txt, msg_type: 'text' });
            document.getElementById('msg-input').value = '';
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

        socket.on('new_msg', (data) => { appendMessage(data); });

        function appendMessage(msg) {
            const box = document.getElementById('messages');
            const isMe = msg.sender === currentUser;
            const div = document.createElement('div');
            div.className = 'message ' + (isMe ? 'msg-me' : 'msg-other');
            
            let contentHtml = `<span>${msg.msg}</span>`;
            if (msg.msg_type === 'file') {
                try {
                    const fileData = JSON.parse(msg.msg);
                    contentHtml = `<a href="data:application/octet-stream;base64,${fileData.data}" download="${fileData.name}" style="color:#fff;">📁 ${fileData.name}</a>`;
                } catch(e) {}
            } else if (msg.msg_type === 'voice') {
                contentHtml = `<audio controls src="data:audio/webm;base64,${msg.msg}" style="height:32px; width:180px;"></audio>`;
            }

            div.innerHTML = `<div class="msg-sender">${isMe ? 'Вы' : msg.sender}</div>${contentHtml}`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        /* --- ЛОГИКА КОМНАТ И МИКРО-ИНДИКАТОРА ГРОМКОСТИ --- */
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
        function leaveActiveRoom() { 
            stopMicMeter();
            socket.emit('mafia_leave', { room_id: activeRoomId, user: currentUser }); 
        }
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
                    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    btn.innerText = '🎙️ Микрофон ВКЛ';
                    btn.style.background = '#388e3c';
                    startMicMeter(mediaStream);
                } catch(err) {
                    alert('Ошибка микрофона: ' + err);
                    isMicOn = false;
                }
            } else {
                stopMicMeter();
                btn.innerText = '🔇 Микрофон ВЫКЛ';
                btn.style.background = '#c62828';
            }
        }

        function startMicMeter(stream) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
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

        function stopMicMeter() {
            if (micAnimFrame) cancelAnimationFrame(micAnimFrame);
            if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
            if (audioContext) audioContext.close();
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
            for (const [uname, pinfo] of Object.entries(data.players)) {
                playerBySlot[pinfo.slot] = { name: uname, ...pinfo };
            }

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

                    slotDiv.innerHTML = `
                        <div class="slot-avatar" style="background:${getAvatarColor(p.name)}">${p.name.charAt(0).toUpperCase()}</div>
                        <div class="slot-name">${p.name}</div>
                        <div class="audio-meter-box" id="meter-box-${p.name}"><div class="audio-meter-bar" id="meter-bar-${p.name}"></div></div>
                        <div class="slot-status">${p.alive ? '🟢 Жив' : '💀 Убит'}</div>
                        ${actBtn}
                    `;
                } else {
                    slotDiv.className = 'table-slot';
                    slotDiv.innerHTML = `
                        <div class="slot-avatar" style="background:#333; color:#666;">+</div>
                        <div class="slot-name" style="color:#777;">Свободно</div>
                    `;
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
def index():
    return render_template_string(HTML_TEMPLATE)

@socketio.on('login')
def on_login(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()

    if row:
        pwd_hash, salt, role = row
        conn.close()
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            join_room(username)
            join_room('#general')
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role})
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
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)", (username, pwd_hash, salt))
        conn.commit()
        emit('auth_response', {'status': 'error', 'message': 'Успешно! Теперь войдите.'})
    except sqlite3.IntegrityError: emit('auth_response', {'status': 'error', 'message': 'Логин занят'})
    finally: conn.close()

@socketio.on('get_channels')
def on_get_channels(): emit('channels_list', {'channels': ['#general']})

@socketio.on('get_users')
def on_get_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role, last_seen FROM users")
    users = [{'username': r[0], 'online': True, 'last_seen': r[2]} for r in cur.fetchall()]
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('send_msg')
def on_send_msg(data):
    target = data['target']
    if target.startswith('#'): emit('new_msg', data, room=target)
    else:
        emit('new_msg', data, room=target)
        if data['sender'] != target: emit('new_msg', data, room=data['sender'])

# SOCKET EVENTS ДЛЯ 5 КОМНАТ МАФИИ
def broadcast_room_state(room_id):
    room = mafia_rooms[room_id]
    emit('mafia_room_update', {
        'room_id': room.room_id,
        'name': room.name,
        'state': room.state,
        'players': room.players,
        'logs': room.logs
    }, broadcast=True)

@socketio.on('mafia_get_rooms')
def on_mafia_get_rooms():
    rinfo = {}
    for rid, r in mafia_rooms.items():
        rinfo[rid] = {'name': r.name, 'state': r.state, 'count': len(r.players)}
    emit('mafia_rooms_list', {'rooms': rinfo})

@socketio.on('mafia_get_room_state')
def on_mafia_get_room_state(data):
    broadcast_room_state(data['room_id'])

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
        if act == 'kill' and room.players[user]['role'] == 'mafia':
            room.night_actions['mafia'][user] = target

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
    socketio.run(app, host='0.0.0.0', port=port)
