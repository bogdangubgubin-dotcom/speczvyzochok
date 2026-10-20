import os
import sqlite3
import hashlib
import time
import json
import psutil
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'speczvyazok_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "messenger.db")

# Пароль администратора: 1234
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

def is_muted(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT until FROM mutes WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if row:
        until = row[0]
        if until == -1 or until > int(time.time()):
            return True, until
    return False, 0

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Спецзвязочок</title>
    
    <link rel="manifest" href="data:application/manifest+json;charset=utf-8,%7B%22name%22%3A%22%D0%A1%D0%BF%D0%B5%D1%86%D0%B7%D0%B2%D1%8F%D0%B7%D0%BE%D1%87%D0%BE%D0%BA%22%2C%22short_name%22%3A%22%D0%A1%D0%BF%D0%B5%D1%86%D0%B7%D0%B2%D1%8F%D0%B7%D0%BE%D1%87%D0%BE%D0%BA%22%2C%22start_url%22%3A%22%2F%22%2C%22display%22%3A%22standalone%22%2C%22background_color%22%3A%22%23121212%22%2C%22theme_color%22%3A%22%231e1e1e%22%7D">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#1e1e1e">

    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        
        html, body { width: 100%; height: 100%; height: 100dvh; overflow: hidden; background: #121212; color: #fff; position: fixed; }
        
        #auth-screen { display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100dvh; padding: 20px; }
        .auth-box { background: #1e1e1e; padding: 25px; border-radius: 16px; width: 100%; max-width: 340px; text-align: center; border: 1px solid #2d2d2d; }
        .auth-box h2 { margin-bottom: 15px; color: #3B8ED0; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin: 8px 0; border-radius: 10px; border: 1px solid #333; background: #2a2a2a; color: #fff; outline: none; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none; background: #1F6AA5; color: #fff; font-weight: bold; cursor: pointer; font-size: 15px; }
        
        #app-screen { display: none; height: 100dvh; width: 100vw; position: relative; overflow: hidden; }
        
        #sidebar { width: 280px; background: #1a1a1a; border-right: 1px solid #2d2d2d; display: flex; flex-direction: column; flex-shrink: 0; }
        .sidebar-header { padding: 12px 15px; background: #1e1e1e; border-bottom: 1px solid #2d2d2d; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
        .section-title { font-size: 11px; text-transform: uppercase; color: #888; margin: 12px 8px 6px 8px; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        
        .chat-item { padding: 8px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; font-size: 14px; background: transparent; transition: background 0.2s; }
        .chat-item:hover, .chat-item.active { background: #2a2a2a; }
        
        .avatar { width: 34px; height: 34px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; font-size: 14px; flex-shrink: 0; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-left: auto; }
        .online { background: #4caf50; }
        .offline { background: #757575; }

        #chat-main { flex: 1; display: flex; flex-direction: column; background: #121212; min-width: 0; height: 100dvh; position: relative; }
        
        header { height: 56px; background: #1e1e1e; padding: 0 15px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2d2d2d; flex-shrink: 0; }
        .header-title-box { display: flex; flex-direction: column; }
        .header-status { font-size: 11px; color: #888; }

        .search-bar { padding: 6px 12px; background: #181818; border-bottom: 1px solid #2d2d2d; display: flex; gap: 8px; }
        .search-bar input { margin: 0; padding: 6px 10px; font-size: 13px; height: 32px; }

        .chat-container { flex: 1; overflow-y: auto; padding: 15px; padding-bottom: 95px; display: flex; flex-direction: column; gap: 10px; -webkit-overflow-scrolling: touch; }
        
        .message { max-width: 80%; padding: 8px 12px; border-radius: 14px; line-height: 1.4; word-wrap: break-word; font-size: 15px; position: relative; display: flex; flex-direction: column; gap: 4px; }
        .msg-me { background: #1F6AA5; align-self: flex-end; border-bottom-right-radius: 2px; }
        .msg-other { background: #2a2a2a; align-self: flex-start; border-bottom-left-radius: 2px; }
        .msg-sender { font-size: 11px; font-weight: bold; color: #82b1ff; }
        .msg-me .msg-sender { color: #e0e0e0; text-align: right; }
        
        .reply-preview-box { background: rgba(0,0,0,0.25); border-left: 3px solid #3B8ED0; padding: 4px 8px; border-radius: 4px; font-size: 12px; color: #ddd; margin-bottom: 4px; cursor: pointer; }
        
        .msg-meta { display: flex; align-items: center; justify-content: flex-end; gap: 4px; font-size: 10px; color: rgba(255,255,255,0.6); margin-top: 2px; }
        .msg-status { font-size: 12px; }
        
        .reactions-row { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
        .reaction-badge { background: rgba(0,0,0,0.3); border-radius: 12px; padding: 2px 6px; font-size: 11px; cursor: pointer; display: flex; align-items: center; gap: 3px; border: 1px solid #444; }

        .msg-actions { display: none; position: absolute; top: -12px; right: 10px; background: #333; border-radius: 16px; padding: 2px 6px; gap: 6px; border: 1px solid #555; z-index: 10; }
        .message:hover .msg-actions { display: flex; }
        .action-btn { cursor: pointer; font-size: 12px; padding: 2px; }

        #typing-indicator { font-size: 12px; color: #888; font-style: italic; padding: 0 15px 5px 15px; height: 18px; }

        #reply-bar { display: none; background: #222; padding: 6px 12px; border-top: 1px solid #333; justify-content: space-between; align-items: center; font-size: 13px; color: #3B8ED0; }

        footer { position: absolute; bottom: 0; left: 0; right: 0; min-height: 60px; background: #1e1e1e; padding: 8px 10px; padding-bottom: max(8px, env(safe-area-inset-bottom)); display: flex; gap: 8px; border-top: 1px solid #2d2d2d; align-items: center; z-index: 99; }
        footer input[type="text"] { flex: 1; margin: 0; height: 42px; background: #2a2a2a; color: #fff; border: 1px solid #333; border-radius: 10px; padding: 0 12px; font-size: 16px; outline: none; }
        footer button { width: auto; padding: 0 14px; margin: 0; height: 42px; }
        .file-btn, .voice-btn { background: #333; border: 1px solid #444; border-radius: 10px; padding: 0 12px; height: 42px; display: flex; align-items: center; justify-content: center; cursor: pointer; color: #fff; flex-shrink: 0; }
        .recording { background: #c62828 !important; animation: pulse 1s infinite; }
        
        .voice-player { display: flex; align-items: center; gap: 8px; width: 220px; }
        .speed-btn { background: #444; border: none; color: #fff; font-size: 10px; padding: 2px 6px; border-radius: 4px; cursor: pointer; }

        .msg-img-preview { max-width: 100%; max-height: 250px; border-radius: 8px; margin-top: 5px; cursor: pointer; object-fit: cover; }

        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .status-lbl { margin-top: 10px; font-size: 13px; color: #ff5555; }
        .logout-btn { background: transparent; border: 1px solid #555; color: #aaa; padding: 4px 8px; font-size: 12px; border-radius: 6px; cursor: pointer; }
        .admin-nav-btn { background: #d32f2f; color: #fff; padding: 6px 10px; font-size: 12px; border-radius: 6px; border: none; font-weight: bold; cursor: pointer; display: none; }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; padding: 15px; }
        .modal-box { background: #1e1e1e; border: 1px solid #333; border-radius: 12px; width: 100%; max-width: 450px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
        .modal-header { padding: 15px; background: #252525; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
        .modal-body { padding: 15px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
        .user-checkbox-item { display: flex; align-items: center; gap: 10px; background: #2a2a2a; padding: 8px 12px; border-radius: 8px; font-size: 14px; cursor: pointer; }

        #lightbox { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.95); z-index: 2000; justify-content: center; align-items: center; }
        #lightbox img { max-width: 95%; max-height: 95%; object-fit: contain; }

        .admin-box { max-width: 600px; }
        .admin-card { background: #2a2a2a; padding: 12px; border-radius: 8px; margin-bottom: 10px; }
        .admin-card h4 { margin-bottom: 8px; color: #3B8ED0; font-size: 14px; }
        .admin-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 5px; }
        .admin-table th, .admin-table td { border: 1px solid #3a3a3a; padding: 6px 8px; text-align: left; }
        .admin-btn { padding: 4px 8px; font-size: 11px; margin: 2px; width: auto; display: inline-block; border-radius: 4px; border: none; color: #fff; font-weight: bold; cursor: pointer; }

        @media (max-width: 600px) {
            #sidebar { width: 100%; position: absolute; z-index: 100; height: 100dvh; display: flex; }
            #sidebar.hidden { display: none; }
            #chat-main { width: 100%; }
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
            <div id="status" class="status-lbl"></div>
        </div>
    </div>

    <div id="app-screen">
        <div id="sidebar">
            <div class="sidebar-header">
                <div style="display:flex; align-items:center; gap:8px; cursor:pointer;" onclick="openProfileModal()">
                    <div id="my-avatar" class="avatar">?</div>
                    <span id="user-display">...</span>
                </div>
                <button class="logout-btn" onclick="logout()">Выйти</button>
            </div>
            <div class="chat-list">
                <div class="section-title">
                    <span>Группы / Каналы</span>
                    <button onclick="openCreateGroupModal()" style="width:auto; padding:2px 8px; margin:0; font-size:16px; background:#1F6AA5;">+</button>
                </div>
                <div id="channels-list"></div>

                <div class="section-title">Личные чаты</div>
                <div id="users-list"></div>
            </div>
        </div>

        <div id="chat-main">
            <header>
                <div style="display:flex; align-items:center; gap:10px;">
                    <button onclick="toggleSidebar()" style="width:auto; padding:6px 10px; margin:0; font-size:12px; background:#333;">☰</button>
                    <div class="header-title-box">
                        <h3 id="chat-title">#general</h3>
                        <span id="chat-status" class="header-status"></span>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <button id="admin-btn" class="admin-nav-btn" onclick="openAdminModal()">⚙️ Админка</button>
                    <span style="font-size:11px; color:#4caf50;">🔒 E2EE</span>
                </div>
            </header>

            <div class="search-bar">
                <input type="text" id="search-input" placeholder="🔍 Поиск по сообщениям..." oninput="searchMessages()">
            </div>

            <div class="chat-container" id="messages"></div>
            <div id="typing-indicator"></div>

            <div id="reply-bar">
                <span id="reply-text">Ответ на сообщение...</span>
                <button onclick="cancelReply()" style="width:auto; padding:2px 8px; background:transparent; border:none; color:#aaa; cursor:pointer;">✕</button>
            </div>

            <footer>
                <label class="file-btn" title="Прикрепить файл">
                    📎
                    <input type="file" id="file-input" style="display:none;" onchange="sendFile()">
                </label>
                <button id="voice-record-btn" class="voice-btn" onclick="toggleVoiceRecord()" title="Голосовое сообщение">🎤</button>
                <input type="text" id="msg-input" placeholder="Сообщение..." oninput="handleTyping()" onkeypress="if(event.key==='Enter') sendMsg()">
                <button onclick="sendMsg()">></button>
            </footer>
        </div>
    </div>

    <!-- Lightbox Просмотр фото -->
    <div id="lightbox" onclick="this.style.display='none'">
        <img id="lightbox-img" src="" alt="Photo">
    </div>

    <!-- Модальное окно профиля -->
    <div id="profile-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>👤 Настройки Профиля</h3>
                <button onclick="closeProfileModal()" style="width:auto; padding:4px 10px; margin:0; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div style="display:flex; justify-content:center; margin-bottom:10px;">
                    <div id="profile-avatar" class="avatar" style="width:64px; height:64px; font-size:24px;">?</div>
                </div>
                <h4>Смена пароля</h4>
                <input type="password" id="old-pass" placeholder="Старый пароль">
                <input type="password" id="new-pass" placeholder="Новый пароль">
                <button onclick="changePassword()">Сохранить новый пароль</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно создания группы -->
    <div id="create-group-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>👥 Создать общий чат</h3>
                <button onclick="closeCreateGroupModal()" style="width:auto; padding:4px 10px; margin:0; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <input type="text" id="group-name-input" placeholder="Название группы (напр. #работа)">
                <div style="font-size:12px; color:#aaa; margin-top:5px;">Отметьте участников:</div>
                <div id="group-users-select" style="display:flex; flex-direction:column; gap:6px; max-height:220px; overflow-y:auto; border:1px solid #333; padding:8px; border-radius:8px;"></div>
                <button onclick="confirmCreateGroup()" style="margin-top:10px;">Создать чат</button>
            </div>
        </div>
    </div>

    <!-- Модальное окно Админки -->
    <div id="admin-modal" class="modal-overlay">
        <div class="modal-box admin-box">
            <div class="modal-header">
                <h3>⚙️ Панель Управления</h3>
                <button onclick="closeAdminModal()" style="width:auto; padding:4px 10px; margin:0; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div class="admin-card">
                    <h4>📊 Нагрузка Сервера</h4>
                    <div id="sys-stats" style="font-size:13px; line-height:1.6; color:#aaa;">Загрузка данных...</div>
                </div>
                <div class="admin-card">
                    <h4>👥 Пользователи и Роли</h4>
                    <table class="admin-table">
                        <thead><tr><th>Юзер</th><th>Роль</th><th>Действия</th></tr></thead>
                        <tbody id="admin-users-table"></tbody>
                    </table>
                </div>
                <div class="admin-card">
                    <h4>💬 Каналы / Группы</h4>
                    <div id="admin-channels-list" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:5px;"></div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let currentUser = "";
        let userRole = "user";
        let currentTarget = "#general";
        let mediaRecorder = null;
        let audioChunks = [];
        let isRecording = false;
        let typingTimeout = null;
        let cachedUsersList = [];
        let activeReplyMsg = null;
        let editingMsgId = null;

        function getAvatarColor(name) {
            let hash = 0;
            for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
            const c = (hash & 0x00FFFFFF).toString(16).toUpperCase();
            return '#' + "00000".substring(0, 6 - c.length) + c;
        }

        function playNotifySound() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(587.33, ctx.currentTime);
                gain.gain.setValueAtTime(0.1, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.00001, ctx.currentTime + 0.2);
                osc.connect(gain); gain.connect(ctx.destination);
                osc.start(); osc.stop(ctx.currentTime + 0.2);
            } catch(e) {}
        }

        function requestPushPermission() {
            if ("Notification" in window && Notification.permission !== "granted") {
                Notification.requestPermission();
            }
        }

        function showPushNotification(title, body) {
            if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
                new Notification(title, { body: body, icon: '/favicon.ico' });
            }
        }

        const msgInput = document.getElementById('msg-input');

        function handleTyping() {
            socket.emit('typing', { sender: currentUser, target: currentTarget });
            clearTimeout(typingTimeout);
            typingTimeout = setTimeout(() => {
                socket.emit('stop_typing', { sender: currentUser, target: currentTarget });
            }, 1500);
        }

        socket.on('user_typing', (data) => {
            if (data.target === currentTarget && data.sender !== currentUser) {
                document.getElementById('typing-indicator').innerText = data.sender + ' печатает...';
            }
        });

        socket.on('user_stop_typing', (data) => {
            if (data.target === currentTarget) {
                document.getElementById('typing-indicator').innerText = '';
            }
        });

        window.addEventListener('DOMContentLoaded', () => {
            requestPushPermission();
            const savedUser = localStorage.getItem('pychat_user');
            const savedPass = localStorage.getItem('pychat_pass');
            if (savedUser && savedPass) {
                socket.emit('login', { username: savedUser, password: savedPass });
            }
        });

        function login() {
            const user = document.getElementById('username').value.trim();
            const pass = document.getElementById('password').value.trim();
            if (!user || !pass) return;
            socket.emit('login', { username: user, password: pass });
        }

        function register() {
            const user = document.getElementById('username').value.trim();
            const pass = document.getElementById('password').value.trim();
            if (!user || !pass) return;
            socket.emit('register', { username: user, password: pass });
        }

        function logout() {
            localStorage.removeItem('pychat_user');
            localStorage.removeItem('pychat_pass');
            location.reload();
        }

        socket.on('auth_response', (data) => {
            if (data.status === 'ok') {
                currentUser = data.username;
                userRole = data.role || 'user';
                
                const pass = document.getElementById('password').value.trim() || localStorage.getItem('pychat_pass');
                localStorage.setItem('pychat_user', currentUser);
                localStorage.setItem('pychat_pass', pass);

                document.getElementById('auth-screen').style.display = 'none';
                document.getElementById('app-screen').style.display = 'flex';
                document.getElementById('user-display').innerText = currentUser + ' [' + userRole + ']';
                
                const myAv = document.getElementById('my-avatar');
                myAv.innerText = currentUser.charAt(0).toUpperCase();
                myAv.style.background = getAvatarColor(currentUser);

                if (userRole === 'admin' || userRole === 'moderator') {
                    document.getElementById('admin-btn').style.display = 'block';
                } else {
                    document.getElementById('admin-btn').style.display = 'none';
                }

                socket.emit('get_channels', { username: currentUser });
                socket.emit('get_users');
                switchTarget('#general');
            } else {
                document.getElementById('status').innerText = data.message;
            }
        });

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('hidden');
        }

        function switchTarget(target) {
            currentTarget = target;
            document.getElementById('chat-title').innerText = target;
            document.getElementById('messages').innerHTML = '';
            document.getElementById('typing-indicator').innerText = '';
            cancelReply();
            
            if (window.innerWidth <= 600) document.getElementById('sidebar').classList.add('hidden');

            const targetUser = cachedUsersList.find(u => u.username === target);
            const statusEl = document.getElementById('chat-status');
            if (targetUser) {
                statusEl.innerText = targetUser.online ? 'в сети' : (targetUser.last_seen ? 'был(а) ' + new Date(targetUser.last_seen*1000).toLocaleTimeString() : 'офлайн');
            } else {
                statusEl.innerText = '';
            }

            socket.emit('get_history', { target: currentTarget, sender: currentUser });
            
            document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
            const activeEl = document.getElementById('chat-' + target.replace('#', 'chan-'));
            if(activeEl) activeEl.classList.add('active');
        }

        function openProfileModal() {
            document.getElementById('profile-avatar').innerText = currentUser.charAt(0).toUpperCase();
            document.getElementById('profile-avatar').style.background = getAvatarColor(currentUser);
            document.getElementById('profile-modal').style.display = 'flex';
        }
        function closeProfileModal() { document.getElementById('profile-modal').style.display = 'none'; }

        function changePassword() {
            const oldP = document.getElementById('old-pass').value;
            const newP = document.getElementById('new-pass').value;
            if(!oldP || !newP) return alert('Заполните все поля');
            socket.emit('change_pass', { username: currentUser, old_pass: oldP, new_pass: newP }, (res) => {
                alert(res.message);
                if(res.status === 'ok') {
                    localStorage.setItem('pychat_pass', newP);
                    closeProfileModal();
                }
            });
        }

        function openCreateGroupModal() {
            const container = document.getElementById('group-users-select');
            container.innerHTML = '';
            cachedUsersList.forEach(u => {
                if (u.username === currentUser) return;
                const label = document.createElement('label');
                label.className = 'user-checkbox-item';
                label.innerHTML = `<input type="checkbox" value="${u.username}"> <div class="avatar" style="width:24px; height:24px; font-size:11px; background:${getAvatarColor(u.username)}">${u.username.charAt(0).toUpperCase()}</div> ${u.username}`;
                container.appendChild(label);
            });
            document.getElementById('create-group-modal').style.display = 'flex';
        }
        function closeCreateGroupModal() { document.getElementById('create-group-modal').style.display = 'none'; }

        function confirmCreateGroup() {
            const nameInput = document.getElementById('group-name-input').value.trim();
            if (!nameInput) return alert('Введите название');
            const selectedUsers = [currentUser];
            document.querySelectorAll('#group-users-select input:checked').forEach(cb => selectedUsers.push(cb.value));
            socket.emit('create_channel', { name: nameInput, creator: currentUser, members: selectedUsers });
            closeCreateGroupModal();
        }

        function sendMsg() {
            const txt = msgInput.value.trim();
            if (!txt) return;

            if (editingMsgId) {
                socket.emit('edit_msg', { msg_id: editingMsgId, new_text: txt, sender: currentUser });
                cancelReply();
                return;
            }

            socket.emit('send_msg', {
                sender: currentUser,
                target: currentTarget,
                msg: txt,
                msg_type: 'text',
                reply_to: activeReplyMsg ? activeReplyMsg.id : null,
                reply_text: activeReplyMsg ? activeReplyMsg.sender + ': ' + activeReplyMsg.text : ''
            });
            msgInput.value = '';
            cancelReply();
            socket.emit('stop_typing', { sender: currentUser, target: currentTarget });
        }

        function replyToMsg(id, sender, text) {
            activeReplyMsg = { id, sender, text };
            document.getElementById('reply-text').innerText = 'Ответ для ' + sender + ': ' + text.substring(0,30);
            document.getElementById('reply-bar').style.display = 'flex';
        }

        function cancelReply() {
            activeReplyMsg = null;
            editingMsgId = null;
            msgInput.value = '';
            document.getElementById('reply-bar').style.display = 'none';
        }

        function editMsg(id, currentText) {
            editingMsgId = id;
            msgInput.value = currentText;
            msgInput.focus();
            document.getElementById('reply-text').innerText = 'Редактирование сообщения...';
            document.getElementById('reply-bar').style.display = 'flex';
        }

        function deleteMsg(id) {
            if (confirm('Удалить сообщение для всех?')) {
                socket.emit('delete_msg', { msg_id: id, sender: currentUser });
            }
        }

        function reactMsg(id, emoji) {
            socket.emit('react_msg', { msg_id: id, emoji: emoji, user: currentUser });
        }

        function sendFile() {
            const file = document.getElementById('file-input').files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = function(e) {
                const b64Data = e.target.result.split(',')[1];
                socket.emit('send_msg', {
                    sender: currentUser, target: currentTarget,
                    msg: JSON.stringify({ name: file.name, data: b64Data }),
                    msg_type: 'file'
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

        socket.on('new_msg', (data) => {
            const isGroupMsg = data.target === currentTarget;
            const isDirectMsg = (data.sender === currentTarget && data.target === currentUser) || (data.sender === currentUser && data.target === currentTarget);

            if (isGroupMsg || isDirectMsg) {
                appendMessage(data);
                if (data.sender !== currentUser) {
                    playNotifySound();
                    showPushNotification('Сообщение от ' + data.sender, data.msg);
                    socket.emit('read_msgs', { target: currentTarget, reader: currentUser });
                }
            }
        });

        socket.on('msg_updated', (data) => {
            const el = document.getElementById('msg-body-' + data.id);
            if (el) el.innerText = data.new_text + ' (изм.)';
        });

        socket.on('msg_deleted', (data) => {
            const el = document.getElementById('msg-box-' + data.id);
            if (el) el.remove();
        });

        socket.on('reactions_updated', (data) => {
            const box = document.getElementById('reactions-' + data.id);
            if (box) {
                box.innerHTML = '';
                for (const [emoji, users] of Object.entries(data.reactions)) {
                    if (users.length > 0) {
                        const badge = document.createElement('span');
                        badge.className = 'reaction-badge';
                        badge.innerText = `${emoji} ${users.length}`;
                        badge.onclick = () => reactMsg(data.id, emoji);
                        box.appendChild(badge);
                    }
                }
            }
        });

        socket.on('history', (data) => {
            const box = document.getElementById('messages');
            box.innerHTML = '';
            if (data.history) {
                data.history.forEach(item => appendMessage(item));
                socket.emit('read_msgs', { target: currentTarget, reader: currentUser });
            }
        });

        function searchMessages() {
            const q = document.getElementById('search-input').value.toLowerCase();
            document.querySelectorAll('.message').forEach(m => {
                m.style.display = m.innerText.toLowerCase().includes(q) ? 'flex' : 'none';
            });
        }

        function appendMessage(msg) {
            const box = document.getElementById('messages');
            const isMe = msg.sender === currentUser;
            const div = document.createElement('div');
            div.className = 'message ' + (isMe ? 'msg-me' : 'msg-other');
            div.id = 'msg-box-' + msg.id;
            
            let contentHtml = `<span id="msg-body-${msg.id}">${msg.msg}</span>`;
            if (msg.msg_type === 'file') {
                try {
                    const fileData = JSON.parse(msg.msg);
                    const ext = fileData.name.split('.').pop().toLowerCase();
                    if (['jpg', 'jpeg', 'png', 'gif', 'webp'].includes(ext)) {
                        contentHtml = `<div>📁 ${fileData.name}</div><img src="data:image/${ext};base64,${fileData.data}" class="msg-img-preview" onclick="openLightbox(this.src)">`;
                    } else if (['mp4', 'webm'].includes(ext)) {
                        contentHtml = `<video controls src="data:video/${ext};base64,${fileData.data}" style="max-width:100%; border-radius:8px;"></video>`;
                    } else {
                        contentHtml = `<a href="data:application/octet-stream;base64,${fileData.data}" download="${fileData.name}" style="color:#fff; text-decoration:underline;">📁 ${fileData.name}</a>`;
                    }
                } catch(e) { contentHtml = '[Файл]'; }
            } else if (msg.msg_type === 'voice') {
                contentHtml = `
                    <div class="voice-player">
                        <audio id="voice-${msg.id}" controls src="data:audio/webm;base64,${msg.msg}" style="height:32px; width:160px;"></audio>
                        <button class="speed-btn" onclick="toggleSpeed('voice-${msg.id}', this)">1x</button>
                    </div>
                `;
            }

            const actionsHtml = `
                <div class="msg-actions">
                    <span class="action-btn" onclick="reactMsg(${msg.id}, '👍')">👍</span>
                    <span class="action-btn" onclick="reactMsg(${msg.id}, '❤️')">❤️</span>
                    <span class="action-btn" onclick="reactMsg(${msg.id}, '🔥')">🔥</span>
                    <span class="action-btn" onclick="replyToMsg(${msg.id}, '${msg.sender}', '${msg.msg}')">↩️</span>
                    ${isMe ? `<span class="action-btn" onclick="editMsg(${msg.id}, '${msg.msg}')">✏️</span>` : ''}
                    ${isMe || userRole === 'admin' ? `<span class="action-btn" onclick="deleteMsg(${msg.id})">🗑️</span>` : ''}
                </div>
            `;

            let replyHtml = msg.reply_text ? `<div class="reply-preview-box">${msg.reply_text}</div>` : '';
            let statusMark = isMe ? (msg.is_read ? '✓✓' : '✓') : '';

            div.innerHTML = `
                ${actionsHtml}
                ${replyHtml}
                <div class="msg-sender">${isMe ? 'Вы' : msg.sender}</div>
                ${contentHtml}
                <div class="reactions-row" id="reactions-${msg.id}"></div>
                <div class="msg-meta">
                    <span>${msg.timestamp ? msg.timestamp.substring(11,16) : ''}</span>
                    <span class="msg-status">${statusMark}</span>
                </div>
            `;

            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
            if (msg.reactions) socket.emit('get_reactions', { msg_id: msg.id });
        }

        function toggleSpeed(audioId, btn) {
            const audio = document.getElementById(audioId);
            if (audio.playbackRate === 1) { audio.playbackRate = 1.5; btn.innerText = '1.5x'; }
            else if (audio.playbackRate === 1.5) { audio.playbackRate = 2; btn.innerText = '2x'; }
            else { audio.playbackRate = 1; btn.innerText = '1x'; }
        }

        function openLightbox(src) {
            document.getElementById('lightbox-img').src = src;
            document.getElementById('lightbox').style.display = 'flex';
        }

        socket.on('channels_list', (data) => {
            const box = document.getElementById('channels-list');
            box.innerHTML = '';
            const adminBox = document.getElementById('admin-channels-list');
            if(adminBox) adminBox.innerHTML = '';

            data.channels.forEach(ch => {
                const div = document.createElement('div');
                div.className = 'chat-item' + (ch === currentTarget ? ' active' : '');
                div.id = 'chat-chan-' + ch.replace('#', '');
                div.innerHTML = `<div class="avatar" style="background:#555;">#</div> ${ch}`;
                div.onclick = () => switchTarget(ch);
                box.appendChild(div);

                if (adminBox && userRole === 'admin') {
                    const chBtn = document.createElement('button');
                    chBtn.className = 'admin-btn';
                    chBtn.style.background = '#d32f2f';
                    chBtn.innerText = '❌ ' + ch;
                    chBtn.onclick = () => deleteChannel(ch);
                    adminBox.appendChild(chBtn);
                }
            });
        });

        socket.on('users_list', (data) => {
            cachedUsersList = data.users;
            const box = document.getElementById('users-list');
            box.innerHTML = '';
            data.users.forEach(u => {
                if(u.username === currentUser) return;
                const div = document.createElement('div');
                div.className = 'chat-item' + (u.username === currentTarget ? ' active' : '');
                div.id = 'chat-' + u.username;
                
                const dot = `<span class="status-dot ${u.online ? 'online' : 'offline'}"></span>`;
                div.innerHTML = `<div class="avatar" style="background:${getAvatarColor(u.username)}">${u.username.charAt(0).toUpperCase()}</div> ${u.username} ${dot}`;
                div.onclick = () => switchTarget(u.username);
                box.appendChild(div);
            });
        });

        function openAdminModal() {
            const pass = prompt("Введите пароль администратора:");
            if (!pass) return;
            socket.emit('admin_verify', { admin: currentUser, pass: pass }, (res) => {
                if (res && res.status === 'ok') {
                    document.getElementById('admin-modal').style.display = 'flex';
                    socket.emit('admin_get_stats');
                    socket.emit('admin_get_users', { admin: currentUser });
                    socket.emit('get_channels', { username: currentUser });
                } else alert('Неверный пароль администратора!');
            });
        }
        function closeAdminModal() { document.getElementById('admin-modal').style.display = 'none'; }

        socket.on('admin_stats', (data) => {
            document.getElementById('sys-stats').innerHTML = `<b>CPU:</b> ${data.cpu}% | <b>ОЗУ:</b> ${data.ram}%`;
        });

        socket.on('admin_users_data', (data) => {
            const tbody = document.getElementById('admin-users-table');
            tbody.innerHTML = '';
            data.users.forEach(u => {
                let actions = `<button class="admin-btn" style="background:#f57c00;" onclick="muteUser('${u.username}')">Мут</button>`;
                if (userRole === 'admin') {
                    actions += ` <button class="admin-btn" style="background:#388e3c;" onclick="setRole('${u.username}', 'moderator')">+Модер</button>`;
                    actions += ` <button class="admin-btn" style="background:#1976d2;" onclick="setRole('${u.username}', 'admin')">+Админ</button>`;
                    actions += ` <button class="admin-btn" style="background:#d32f2f;" onclick="deleteUser('${u.username}')">Удалить</button>`;
                }
                tbody.innerHTML += `<tr><td><b>${u.username}</b></td><td>${u.role}</td><td>${actions}</td></tr>`;
            });
        });

        function muteUser(t) {
            const m = prompt("Мут в минутах (0 - навсегда):", "60");
            if(m !== null) socket.emit('admin_mute', { admin: currentUser, target: t, minutes: parseInt(m) });
        }

        function setRole(t, r) {
            socket.emit('admin_set_role', { admin: currentUser, target: t, role: r });
        }

        function deleteUser(t) {
            if(confirm(`Удалить ${t}?`)) socket.emit('admin_delete_user', { admin: currentUser, target: t });
        }

        function deleteChannel(n) {
            if(confirm(`Удалить канал ${n}?`)) socket.emit('admin_delete_channel', { admin: currentUser, name: n });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@socketio.on('typing')
def on_typing(data):
    emit('user_typing', data, broadcast=True)

@socketio.on('stop_typing')
def on_stop_typing(data):
    emit('user_stop_typing', data, broadcast=True)

@socketio.on('register')
def on_register(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        emit('auth_response', {'status': 'error', 'message': 'Заполните поля'})
        return

    role = 'admin' if username == 'totr' else 'user'
    pwd_hash, salt = hash_password(password)
    
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)", (username, pwd_hash, salt, role))
        conn.commit()
        emit('auth_response', {'status': 'error', 'message': 'Успешно! Теперь войдите.'})
    except sqlite3.IntegrityError:
        emit('auth_response', {'status': 'error', 'message': 'Логин занят'})
    finally:
        conn.close()

@socketio.on('login')
def on_login(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()

    if row:
        pwd_hash, salt, role = row
        if username == 'totr' and role != 'admin':
            role = 'admin'
            cur.execute("UPDATE users SET role = 'admin' WHERE username = 'totr'")
            conn.commit()

        cur.execute("UPDATE users SET last_seen = ? WHERE username = ?", (int(time.time()), username))
        conn.commit()
        conn.close()
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role})
        else:
            emit('auth_response', {'status': 'error', 'message': 'Неверный пароль'})
    else:
        conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Пользователь не найден'})

@socketio.on('change_pass')
def on_change_pass(data):
    username = data['username']
    old_p = data['old_pass']
    new_p = data['new_pass']
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if row:
        pwd_hash, salt = row
        check_hash, _ = hash_password(old_p, salt)
        if check_hash == pwd_hash:
            new_hash, new_salt = hash_password(new_p)
            cur.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?", (new_hash, new_salt, username))
            conn.commit()
            conn.close()
            return {'status': 'ok', 'message': 'Пароль успешно изменен!'}
    conn.close()
    return {'status': 'error', 'message': 'Неверный старый пароль'}

@socketio.on('get_channels')
def on_get_channels(data=None):
    username = data.get('username', '') if data else ''
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, members FROM channels ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    channels = []
    for name, members_str in rows:
        if name == '#general' or not members_str:
            channels.append(name)
        else:
            try:
                members = json.loads(members_str)
                if username in members: channels.append(name)
            except Exception: channels.append(name)

    emit('channels_list', {'channels': channels}, broadcast=True)

@socketio.on('get_users')
def on_get_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role, last_seen FROM users")
    users = [{'username': r[0], 'online': True, 'last_seen': r[2]} for r in cur.fetchall()]
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('create_channel')
def on_create_channel(data):
    name = data.get('name', '').strip()
    creator = data.get('creator', '')
    members = data.get('members', [])
    if not name.startswith('#'): name = '#' + name
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO channels (name, creator, members) VALUES (?, ?, ?)", (name, creator, json.dumps(members)))
        conn.commit()
        conn.close()
        on_get_channels({'username': creator})
    except sqlite3.IntegrityError: conn.close()

@socketio.on('send_msg')
def on_send_msg(data):
    sender = data['sender']
    target = data['target']
    msg = data['msg']
    msg_type = data.get('msg_type', 'text')
    reply_to = data.get('reply_to')
    reply_text = data.get('reply_text', '')

    muted, until = is_muted(sender)
    if muted:
        emit('error_msg', {'message': 'Вам заблокирована отправка сообщений (Мут).'})
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (sender, target, message, msg_type, reply_to, reply_text) VALUES (?, ?, ?, ?, ?, ?)", 
                (sender, target, msg, msg_type, reply_to, reply_text))
    msg_id = cur.lastrowid
    cur.execute("SELECT timestamp FROM messages WHERE id = ?", (msg_id,))
    ts = cur.fetchone()[0]
    conn.commit()
    conn.close()

    payload = {
        'id': msg_id, 'sender': sender, 'target': target, 'msg': msg, 
        'msg_type': msg_type, 'reply_to': reply_to, 'reply_text': reply_text,
        'timestamp': ts, 'is_read': 0, 'reactions': {}
    }
    emit('new_msg', payload, broadcast=True)

@socketio.on('edit_msg')
def on_edit_msg(data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET message = ?, is_edited = 1 WHERE id = ? AND sender = ?", 
                (data['new_text'], data['msg_id'], data['sender']))
    conn.commit()
    conn.close()
    emit('msg_updated', {'id': data['msg_id'], 'new_text': data['new_text']}, broadcast=True)

@socketio.on('delete_msg')
def on_delete_msg(data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id = ?", (data['msg_id'],))
    conn.commit()
    conn.close()
    emit('msg_deleted', {'id': data['msg_id']}, broadcast=True)

@socketio.on('react_msg')
def on_react_msg(data):
    msg_id = data['msg_id']
    emoji = data['emoji']
    user = data['user']

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT reactions FROM messages WHERE id = ?", (msg_id,))
    row = cur.fetchone()
    if row:
        reactions = json.loads(row[0] or '{}')
        if emoji not in reactions: reactions[emoji] = []
        if user in reactions[emoji]: reactions[emoji].remove(user)
        else: reactions[emoji].append(user)
        cur.execute("UPDATE messages SET reactions = ? WHERE id = ?", (json.dumps(reactions), msg_id))
        conn.commit()
        emit('reactions_updated', {'id': msg_id, 'reactions': reactions}, broadcast=True)
    conn.close()

@socketio.on('read_msgs')
def on_read_msgs(data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET is_read = 1 WHERE target = ? AND sender != ?", (data['target'], data['reader']))
    conn.commit()
    conn.close()

@socketio.on('get_history')
def on_get_history(data):
    target = data['target']
    sender = data.get('sender', '')
    conn = get_db()
    cur = conn.cursor()
    
    if target.startswith('#'):
        cur.execute("SELECT id, sender, message, msg_type, reply_to, reply_text, timestamp, is_read, reactions FROM messages WHERE target = ? ORDER BY id ASC LIMIT 50", (target,))
    else:
        cur.execute("""
            SELECT id, sender, message, msg_type, reply_to, reply_text, timestamp, is_read, reactions FROM messages 
            WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
            ORDER BY id ASC LIMIT 50
        """, (sender, target, target, sender))
        
    rows = cur.fetchall()
    conn.close()
    history = [{
        'id': r[0], 'sender': r[1], 'msg': r[2], 'msg_type': r[3],
        'reply_to': r[4], 'reply_text': r[5], 'timestamp': r[6],
        'is_read': r[7], 'reactions': json.loads(r[8] or '{}')
    } for r in rows]
    emit('history', {'target': target, 'history': history})

def check_admin_perm(username, require_admin=False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row: return False
    role = row[0]
    if require_admin: return role == 'admin'
    return role in ['admin', 'moderator']

@socketio.on('admin_verify')
def on_admin_verify(data):
    pass_input = data.get('pass', '')
    pass_hash = hashlib.sha256(pass_input.encode('utf-8')).hexdigest()
    if pass_hash == ADMIN_PASSWORD_HASH and check_admin_perm(data.get('admin')):
        return {'status': 'ok'}
    return {'status': 'error'}

@socketio.on('admin_get_stats')
def on_admin_stats():
    emit('admin_stats', {'cpu': psutil.cpu_percent(), 'ram': psutil.virtual_memory().percent})

@socketio.on('admin_get_users')
def on_admin_get_users(data):
    if not check_admin_perm(data.get('admin')): return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users")
    users = [{'username': r[0], 'role': r[1]} for r in cur.fetchall()]
    conn.close()
    emit('admin_users_data', {'users': users})

@socketio.on('admin_mute')
def on_admin_mute(data):
    if not check_admin_perm(data.get('admin')): return
    until = -1 if data.get('minutes', 60) == 0 else int(time.time()) + (data.get('minutes', 60) * 60)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO mutes (username, until) VALUES (?, ?)", (data.get('target'), until))
    conn.commit()
    conn.close()

@socketio.on('admin_set_role')
def on_admin_set_role(data):
    admin = data.get('admin')
    target = data.get('target')
    role = data.get('role', 'user')
    if not check_admin_perm(admin, require_admin=True): return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = ? WHERE username = ?", (role, target))
    conn.commit()
    conn.close()
    on_admin_get_users({'admin': admin})

@socketio.on('admin_delete_user')
def on_admin_delete_user(data):
    admin = data.get('admin')
    target = data.get('target')
    if not check_admin_perm(admin, require_admin=True): return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE username = ?", (target,))
    cur.execute("DELETE FROM mutes WHERE username = ?", (target,))
    conn.commit()
    conn.close()
    on_admin_get_users({'admin': admin})
    on_get_users()

@socketio.on('admin_delete_channel')
def on_admin_delete_channel(data):
    admin = data.get('admin')
    name = data.get('name')
    if not check_admin_perm(admin, require_admin=True): return
    if name == '#general': return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    on_get_channels({'username': admin})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
