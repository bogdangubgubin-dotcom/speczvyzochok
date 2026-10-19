import os
import sqlite3
import hashlib
import time
import psutil
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pychat_admin_secret_key_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "messenger.db")

ADMIN_PASSWORD_HASH = hashlib.sha256('12345'.encode('utf-8')).hexdigest()

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
            role TEXT DEFAULT 'user'
        )
    """)
    
    cur.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cur.fetchall()]
    if 'role' not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            creator TEXT NOT NULL
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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, interactive-widget=resizes-content">
    <title>PyChat Mobile & Admin</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        
        html, body { 
            width: 100%; 
            height: 100%; 
            height: 100dvh; 
            overflow: hidden; 
            background: #121212; 
            color: #fff; 
            position: fixed;
        }
        
        #auth-screen { display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100dvh; padding: 20px; }
        .auth-box { background: #1e1e1e; padding: 25px; border-radius: 16px; width: 100%; max-width: 340px; text-align: center; border: 1px solid #2d2d2d; }
        .auth-box h2 { margin-bottom: 15px; color: #3B8ED0; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin: 8px 0; border-radius: 10px; border: 1px solid #333; background: #2a2a2a; color: #fff; outline: none; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none; background: #1F6AA5; color: #fff; font-weight: bold; cursor: pointer; font-size: 15px; }
        
        #app-screen { 
            display: none; 
            height: 100dvh; 
            width: 100vw; 
            position: relative; 
            overflow: hidden;
        }
        
        #sidebar { width: 280px; background: #1a1a1a; border-right: 1px solid #2d2d2d; display: flex; flex-direction: column; flex-shrink: 0; }
        .sidebar-header { padding: 15px; background: #1e1e1e; border-bottom: 1px solid #2d2d2d; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
        .section-title { font-size: 11px; text-transform: uppercase; color: #888; margin: 12px 8px 6px 8px; font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        
        .chat-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; font-size: 14px; background: transparent; transition: background 0.2s; }
        .chat-item:hover, .chat-item.active { background: #2a2a2a; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .online { background: #4caf50; }
        .offline { background: #757575; }

        #chat-main { 
            flex: 1; 
            display: flex; 
            flex-direction: column; 
            background: #121212; 
            min-width: 0; 
            height: 100dvh;
            position: relative;
        }
        
        header { 
            height: 56px;
            background: #1e1e1e; 
            padding: 0 15px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 1px solid #2d2d2d; 
            flex-shrink: 0; 
        }
        
        .chat-container { 
            flex: 1; 
            overflow-y: auto; 
            padding: 15px; 
            padding-bottom: 75px;
            display: flex; 
            flex-direction: column; 
            gap: 10px; 
            -webkit-overflow-scrolling: touch; 
        }
        
        .message { max-width: 80%; padding: 10px 14px; border-radius: 14px; line-height: 1.4; word-wrap: break-word; font-size: 15px; }
        .msg-me { background: #1F6AA5; align-self: flex-end; border-bottom-right-radius: 2px; }
        .msg-other { background: #2a2a2a; align-self: flex-start; border-bottom-left-radius: 2px; }
        .msg-sender { font-size: 11px; opacity: 0.6; margin-bottom: 3px; font-weight: bold; }

        footer { 
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            min-height: 60px;
            background: #1e1e1e; 
            padding: 8px 10px; 
            padding-bottom: max(8px, env(safe-area-inset-bottom));
            display: flex; 
            gap: 8px; 
            border-top: 1px solid #2d2d2d; 
            align-items: center; 
            z-index: 99; 
        }
        
        footer input[type="text"] { 
            flex: 1; 
            margin: 0; 
            height: 42px;
            background: #2a2a2a;
            color: #fff;
            border: 1px solid #333;
            border-radius: 10px;
            padding: 0 12px;
            font-size: 16px;
            outline: none;
        }
        
        footer button { width: auto; padding: 0 14px; margin: 0; height: 42px; }
        .file-btn, .voice-btn { background: #333; border: 1px solid #444; border-radius: 10px; padding: 0 12px; height: 42px; display: flex; align-items: center; justify-content: center; cursor: pointer; color: #fff; flex-shrink: 0; }
        .recording { background: #c62828 !important; animation: pulse 1s infinite; }
        
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .status-lbl { margin-top: 10px; font-size: 13px; color: #ff5555; }
        .logout-btn { background: transparent; border: 1px solid #555; color: #aaa; padding: 4px 8px; font-size: 12px; border-radius: 6px; cursor: pointer; }
        .admin-nav-btn { background: #d32f2f; color: #fff; padding: 6px 10px; font-size: 12px; border-radius: 6px; border: none; font-weight: bold; cursor: pointer; display: none; }

        #admin-modal { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; padding: 15px; }
        .admin-box { background: #1e1e1e; border: 1px solid #333; border-radius: 12px; width: 100%; max-width: 600px; max-height: 90vh; display: flex; flex-direction: column; overflow: hidden; }
        .admin-header { padding: 15px; background: #252525; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
        .admin-content { padding: 15px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 15px; }
        .admin-card { background: #2a2a2a; padding: 12px; border-radius: 8px; }
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
            <h2>PyChat Mobile</h2>
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
                <span id="user-display">👤 ...</span>
                <button class="logout-btn" onclick="logout()">Выйти</button>
            </div>
            <div class="chat-list">
                <div class="section-title">
                    Группы
                    <span style="cursor:pointer; font-size:16px;" onclick="createChannel()">+</span>
                </div>
                <div id="channels-list"></div>

                <div class="section-title">Чаты</div>
                <div id="users-list"></div>
            </div>
        </div>

        <div id="chat-main">
            <header>
                <div style="display:flex; align-items:center; gap:10px;">
                    <button onclick="toggleSidebar()" style="width:auto; padding:6px 10px; margin:0; font-size:12px; background:#333;">☰</button>
                    <h3 id="chat-title">#general</h3>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <button id="admin-btn" class="admin-nav-btn" onclick="openAdminModal()">⚙️ Админка</button>
                    <span style="font-size:11px; color:#4caf50;">🔒 E2EE</span>
                </div>
            </header>

            <div class="chat-container" id="messages"></div>

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

    <div id="admin-modal">
        <div class="admin-box">
            <div class="admin-header">
                <h3>⚙️ Панель Управления</h3>
                <button onclick="closeAdminModal()" style="width:auto; padding:4px 10px; margin:0; background:#444;">✕</button>
            </div>
            <div class="admin-content">
                <div class="admin-card" id="admin-stats-card">
                    <h4>📊 Нагрузка Сервера</h4>
                    <div id="sys-stats" style="font-size:13px; line-height:1.6; color:#aaa;">Загрузка данных...</div>
                </div>

                <div class="admin-card">
                    <h4>👥 Пользователи и Разрешения</h4>
                    <table class="admin-table">
                        <thead>
                            <tr><th>Юзер</th><th>Роль</th><th>Действия</th></tr>
                        </thead>
                        <tbody id="admin-users-table"></tbody>
                    </table>
                </div>

                <div class="admin-card" id="admin-channels-sec">
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

        const msgInput = document.getElementById('msg-input');
        
        msgInput.addEventListener('focus', () => {
            setTimeout(() => {
                window.scrollTo(0, 0);
                const box = document.getElementById('messages');
                box.scrollTop = box.scrollHeight;
            }, 300);
        });

        window.addEventListener('DOMContentLoaded', () => {
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
                document.getElementById('user-display').innerText = '👤 ' + currentUser + ' [' + userRole + ']';
                
                if (userRole === 'admin' || userRole === 'moderator') {
                    document.getElementById('admin-btn').style.display = 'block';
                }

                socket.emit('get_channels');
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
            
            if (window.innerWidth <= 600) {
                document.getElementById('sidebar').classList.add('hidden');
            }

            socket.emit('get_history', { target: currentTarget, sender: currentUser });
            
            document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
            const activeEl = document.getElementById('chat-' + target.replace('#', 'chan-'));
            if(activeEl) activeEl.classList.add('active');
        }

        function createChannel() {
            const name = prompt("Введите название новой группы:");
            if (name && name.trim()) {
                socket.emit('create_channel', { name: name.trim(), creator: currentUser });
            }
        }

        function sendMsg() {
            const txt = msgInput.value.trim();
            if (!txt) return;

            socket.emit('send_msg', {
                sender: currentUser,
                target: currentTarget,
                msg: txt,
                msg_type: 'text'
            });
            msgInput.value = '';
        }

        function sendFile() {
            const fileInput = document.getElementById('file-input');
            const file = fileInput.files[0];
            if (!file) return;

            const reader = new FileReader();
            reader.onload = function(e) {
                const b64Data = e.target.result.split(',')[1];
                const payload = JSON.stringify({ name: file.name, data: b64Data });
                socket.emit('send_msg', {
                    sender: currentUser,
                    target: currentTarget,
                    msg: payload,
                    msg_type: 'file'
                });
                fileInput.value = '';
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
                            const b64Data = reader.result.split(',')[1];
                            socket.emit('send_msg', {
                                sender: currentUser,
                                target: currentTarget,
                                msg: b64Data,
                                msg_type: 'voice'
                            });
                        };
                        reader.readAsDataURL(audioBlob);
                    };

                    mediaRecorder.start();
                    isRecording = true;
                    btn.classList.add('recording');
                } catch (err) {
                    alert('Ошибка доступа к микрофону: ' + err);
                }
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
                appendMessage(data.sender, data.msg, data.msg_type);
            }
        });

        socket.on('history', (data) => {
            const box = document.getElementById('messages');
            box.innerHTML = '';
            if (data.history) {
                data.history.forEach(item => {
                    appendMessage(item[0], item[1], item[2] || 'text');
                });
            }
        });

        socket.on('channels_list', (data) => {
            const box = document.getElementById('channels-list');
            box.innerHTML = '';
            const adminBox = document.getElementById('admin-channels-list');
            if(adminBox) adminBox.innerHTML = '';

            data.channels.forEach(ch => {
                const div = document.createElement('div');
                div.className = 'chat-item' + (ch === currentTarget ? ' active' : '');
                div.id = 'chat-chan-' + ch.replace('#', '');
                div.innerText = ch;
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
            const box = document.getElementById('users-list');
            box.innerHTML = '';
            data.users.forEach(u => {
                if(u.username === currentUser) return;
                const div = document.createElement('div');
                div.className = 'chat-item' + (u.username === currentTarget ? ' active' : '');
                div.id = 'chat-' + u.username;
                
                const dot = document.createElement('span');
                dot.className = 'status-dot ' + (u.online ? 'online' : 'offline');
                
                div.appendChild(dot);
                div.appendChild(document.createTextNode(' ' + u.username));
                div.onclick = () => switchTarget(u.username);
                box.appendChild(div);
            });
        });

        socket.on('error_msg', (data) => {
            alert(data.message);
        });

        function appendMessage(sender, text, msgType = 'text') {
            const box = document.getElementById('messages');
            const isMe = sender === currentUser;
            const div = document.createElement('div');
            div.className = 'message ' + (isMe ? 'msg-me' : 'msg-other');
            
            let contentHtml = text;
            if (msgType === 'file') {
                try {
                    const fileData = JSON.parse(text);
                    contentHtml = `<a href="data:application/octet-stream;base64,${fileData.data}" download="${fileData.name}" style="color:#fff; text-decoration:underline;">📁 ${fileData.name}</a>`;
                } catch(e) { contentHtml = '[Файл]'; }
            } else if (msgType === 'voice') {
                contentHtml = `<audio controls src="data:audio/webm;base64,${text}" style="max-width:200px; height:36px;"></audio>`;
            }

            div.innerHTML = `<div class="msg-sender">${sender}</div>${contentHtml}`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function openAdminModal() {
            const pass = prompt("Введите пароль администратора:");
            if (!pass) return;

            socket.emit('admin_verify', { admin: currentUser, pass: pass }, (res) => {
                if (res && res.status === 'ok') {
                    document.getElementById('admin-modal').style.display = 'flex';
                    socket.emit('admin_get_stats');
                    socket.emit('admin_get_users', { admin: currentUser });
                    socket.emit('get_channels');
                } else {
                    alert('Неверный пароль администратора!');
                }
            });
        }

        function closeAdminModal() {
            document.getElementById('admin-modal').style.display = 'none';
        }

        socket.on('admin_stats', (data) => {
            document.getElementById('sys-stats').innerHTML = 
                `<b>Загрузка CPU:</b> ${data.cpu}%<br><b>Загрузка ОЗУ:</b> ${data.ram}%<br><b>Соединений:</b> ${data.connections}`;
        });

        socket.on('admin_users_data', (data) => {
            const tbody = document.getElementById('admin-users-table');
            tbody.innerHTML = '';
            data.users.forEach(u => {
                const tr = document.createElement('tr');
                let actions = `<button class="admin-btn" style="background:#f57c00;" onclick="muteUser('${u.username}')">Мут</button>`;
                
                if (userRole === 'admin') {
                    actions += ` <button class="admin-btn" style="background:#388e3c;" onclick="setRole('${u.username}', 'moderator')">+Модер</button>`;
                    actions += ` <button class="admin-btn" style="background:#1976d2;" onclick="setRole('${u.username}', 'admin')">+Админ</button>`;
                    actions += ` <button class="admin-btn" style="background:#d32f2f;" onclick="deleteUser('${u.username}')">Удалить</button>`;
                }

                tr.innerHTML = `<td><b>${u.username}</b></td><td>${u.role}</td><td>${actions}</td>`;
                tbody.appendChild(tr);
            });
        });

        function muteUser(target) {
            const mins = prompt("Введите время мута в минутах (0 - навсегда):", "60");
            if (mins !== null) {
                socket.emit('admin_mute', { admin: currentUser, target: target, minutes: parseInt(mins) });
            }
        }

        function setRole(target, role) {
            socket.emit('admin_set_role', { admin: currentUser, target: target, role: role });
        }

        function deleteUser(target) {
            if (confirm(`Удалить аккаунт ${target}?`)) {
                socket.emit('admin_delete_user', { admin: currentUser, target: target });
            }
        }

        function deleteChannel(name) {
            if (confirm(`Удалить канал ${name}?`)) {
                socket.emit('admin_delete_channel', { admin: currentUser, name: name });
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

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
        cur.execute("INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)", 
                    (username, pwd_hash, salt, role))
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
            
        conn.close()
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role})
        else:
            emit('auth_response', {'status': 'error', 'message': 'Неверный пароль'})
    else:
        conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Пользователь не найден'})

@socketio.on('get_channels')
def on_get_channels():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM channels ORDER BY id ASC")
    channels = [r[0] for r in cur.fetchall()]
    conn.close()
    emit('channels_list', {'channels': channels})

@socketio.on('get_users')
def on_get_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users")
    users = [{'username': r[0], 'online': True} for r in cur.fetchall()]
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('create_channel')
def on_create_channel(data):
    name = data.get('name', '').strip()
    creator = data.get('creator', '')
    if not name.startswith('#'):
        name = '#' + name
        
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO channels (name, creator) VALUES (?, ?)", (name, creator))
        conn.commit()
        conn.close()
        on_get_channels()
    except sqlite3.IntegrityError:
        conn.close()

@socketio.on('send_msg')
def on_send_msg(data):
    sender = data['sender']
    target = data['target']
    msg = data['msg']
    msg_type = data.get('msg_type', 'text')

    muted, until = is_muted(sender)
    if muted:
        emit('error_msg', {'message': 'Вам заблокирована отправка сообщений (Мут).'})
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (sender, target, message, msg_type) VALUES (?, ?, ?, ?)", 
                (sender, target, msg, msg_type))
    conn.commit()
    conn.close()

    payload = {'sender': sender, 'target': target, 'msg': msg, 'msg_type': msg_type}
    emit('new_msg', payload, broadcast=True)

@socketio.on('get_history')
def on_get_history(data):
    target = data['target']
    sender = data.get('sender', '')
    conn = get_db()
    cur = conn.cursor()
    
    if target.startswith('#'):
        cur.execute("SELECT sender, message, msg_type FROM messages WHERE target = ? ORDER BY id ASC LIMIT 50", (target,))
    else:
        cur.execute("""
            SELECT sender, message, msg_type FROM messages 
            WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) 
            ORDER BY id ASC LIMIT 50
        """, (sender, target, target, sender))
        
    history = cur.fetchall()
    conn.close()
    emit('history', {'target': target, 'history': history})

def check_admin_perm(username, require_admin=False):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row: return False
    role = row[0]
    if require_admin:
        return role == 'admin'
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
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    emit('admin_stats', {'cpu': cpu, 'ram': ram, 'connections': 1})

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
    admin = data.get('admin')
    target = data.get('target')
    minutes = data.get('minutes', 60)
    
    if not check_admin_perm(admin): return

    until = -1 if minutes == 0 else int(time.time()) + (minutes * 60)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO mutes (username, until) VALUES (?, ?)", (target, until))
    conn.commit()
    conn.close()
    emit('error_msg', {'message': f'Пользователь {target} замучен.'})

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

    if name == '#general':
        emit('error_msg', {'message': 'Нельзя удалить основной канал!'})
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    on_get_channels()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
