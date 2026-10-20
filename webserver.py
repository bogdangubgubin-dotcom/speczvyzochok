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

# --- ДВИЖОК МАФИИ С ПОДДЕРЖКОЙ ГОЛОСОВОГО ЧАТА ---
class MafiaGame:
    def __init__(self):
        self.state = 'lobby' # lobby, night, day, voting, ended
        self.players = {} # {username: {'role': 'mafia'/'citizen'/'doc'/'cop', 'alive': True, 'mic': True}}
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        self.day_votes = {} # {voter: target}
        self.logs = []

    def add_player(self, username):
        if self.state == 'lobby' and username not in self.players:
            self.players[username] = {'role': None, 'alive': True, 'mic': True}
            return True
        return False

    def remove_player(self, username):
        if self.state == 'lobby' and username in self.players:
            del self.players[username]
            return True
        return False

    def start_game(self):
        plist = list(self.players.keys())
        if len(plist) < 3:
            return False, "Нужно минимум 3 игрока!"

        random.shuffle(plist)
        mafia_count = max(1, len(plist) // 3)
        
        roles = ['mafia'] * mafia_count
        if len(plist) >= 4:
            roles.append('doc')
        if len(plist) >= 5:
            roles.append('cop')
        
        while len(roles) < len(plist):
            roles.append('citizen')
        
        random.shuffle(roles)

        for i, u in enumerate(plist):
            self.players[u]['role'] = roles[i]
            self.players[u]['alive'] = True
            self.players[u]['mic'] = False # Ночью микрофоны глушатся

        self.state = 'night'
        self.night_actions = {'mafia': {}, 'doc': None, 'cop': None}
        self.day_votes = {}
        self.logs = ["Игра началась! Наступила первая ночь. Микрофоны отключены."]
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
        killed = None
        if mafia_targets:
            killed = max(set(mafia_targets), key=mafia_targets.count)

        saved = self.night_actions['doc']
        
        msg = "Наступило утро. "
        if killed and killed != saved:
            self.players[killed]['alive'] = False
            self.players[killed]['mic'] = False
            msg += f"Ночью был убит игрок **{killed}**."
        else:
            msg += "Ночью никто не погиб (Доктор спас жертву или мафия промахнулась)!"

        self.logs.append(msg)
        win = self.check_win()
        if win:
            self.logs.append(f"🎉 **ИГРА ОКОНЧЕНА! {win}**")
        else:
            self.state = 'day'
            # Включаем микрофоны живым игрокам на дневную фазу
            for u, p in self.players.items():
                if p['alive']:
                    p['mic'] = True

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
            self.logs.append(f"По результатам голосования город выгнал игрока **{exiled}** ({self.players[exiled]['role']}).")

        win = self.check_win()
        if win:
            self.logs.append(f"🎉 **ИГРА ОКОНЧЕНА! {win}**")
        else:
            self.state = 'night'
            self.logs.append("Наступает ночь... Город засыпает.")
            # Отключаем микрофоны
            for u, p in self.players.items():
                p['mic'] = False

        self.day_votes = {}

mafia_game = MafiaGame()

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
        .chat-item { padding: 8px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; font-size: 14px; }
        
        .avatar { width: 34px; height: 34px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; font-size: 14px; flex-shrink: 0; }

        #chat-main { flex: 1; display: flex; flex-direction: column; background: #121212; min-width: 0; height: 100dvh; position: relative; }
        header { height: 56px; background: #1e1e1e; padding: 0 15px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2d2d2d; }
        .chat-container { flex: 1; overflow-y: auto; padding: 15px; padding-bottom: 95px; display: flex; flex-direction: column; gap: 10px; }
        .message { max-width: 80%; padding: 8px 12px; border-radius: 14px; font-size: 15px; }
        .msg-me { background: #1F6AA5; align-self: flex-end; }
        .msg-other { background: #2a2a2a; align-self: flex-start; }

        footer { position: absolute; bottom: 0; left: 0; right: 0; min-height: 60px; background: #1e1e1e; padding: 8px 10px; display: flex; gap: 8px; align-items: center; }
        footer input[type="text"] { flex: 1; height: 42px; background: #2a2a2a; color: #fff; border: 1px solid #333; border-radius: 10px; padding: 0 12px; }

        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100dvh; background: rgba(0,0,0,0.85); z-index: 1000; justify-content: center; align-items: center; padding: 15px; }
        .modal-box { background: #1e1e1e; border: 1px solid #333; border-radius: 12px; width: 100%; max-width: 450px; max-height: 90vh; display: flex; flex-direction: column; }
        .modal-header { padding: 15px; background: #252525; border-bottom: 1px solid #333; display: flex; justify-content: space-between; align-items: center; }
        .modal-body { padding: 15px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }

        .voice-box { background: #222; border: 1px solid #333; padding: 10px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; }
        .voice-status { font-size: 12px; color: #4caf50; font-weight: bold; }
        .player-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #333; }
        .act-btn { padding: 4px 8px; font-size: 11px; background: #d32f2f; border: none; border-radius: 4px; color: #fff; cursor: pointer; }

        @media (max-width: 600px) {
            #sidebar { width: 100%; position: absolute; z-index: 100; height: 100dvh; display: flex; }
            #sidebar.hidden { display: none; }
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
        </div>
    </div>

    <div id="app-screen">
        <div id="sidebar">
            <div class="sidebar-header">
                <span id="user-display">...</span>
                <button onclick="logout()">Выйти</button>
            </div>
            <div class="chat-list">
                <div class="section-title">Каналы</div>
                <div id="channels-list"></div>
                <div class="section-title">Пользователи</div>
                <div id="users-list"></div>
            </div>
        </div>

        <div id="chat-main">
            <header>
                <button onclick="toggleSidebar()">☰</button>
                <h3 id="chat-title">#general</h3>
                <button style="background:#8e24aa; color:#fff; border:none; padding:6px 12px; border-radius:6px;" onclick="openMafiaModal()">🕵️ Мафия & Голос</button>
            </header>

            <div class="chat-container" id="messages"></div>

            <footer>
                <input type="text" id="msg-input" placeholder="Сообщение..." onkeypress="if(event.key==='Enter') sendMsg()">
                <button onclick="sendMsg()">></button>
            </footer>
        </div>
    </div>

    <!-- ОКНО МАФИИ И ГОЛОСОВОГО ЧАТА -->
    <div id="mafia-modal" class="modal-overlay">
        <div class="modal-box">
            <div class="modal-header">
                <h3>🕵️ Мафия + Голосовой чат</h3>
                <button onclick="closeMafiaModal()" style="width:auto; padding:4px 10px; background:#444;">✕</button>
            </div>
            <div class="modal-body">
                <div class="voice-box">
                    <div>🎙️ Голосовая связь: <span id="voice-indicator" class="voice-status">Заглушена (Ночь)</span></div>
                    <button id="mic-btn" onclick="toggleMic()" style="width:auto; padding:4px 10px; font-size:12px;" disabled>Вкл Микрофон</button>
                </div>

                <div>
                    <b>Статус:</b> <span id="mafia-state-text" style="color:#3B8ED0;">Лобби</span> | 
                    <b>Моя роль:</b> <span id="my-role-badge">Скрыта</span> 
                    <button onclick="toggleRoleShow()" style="width:auto; padding:2px 6px; font-size:10px;">👁</button>
                </div>

                <div style="display:flex; gap:8px;">
                    <button onclick="joinMafiaGame()">Войти в игру</button>
                    <button onclick="startMafiaGame()" style="background:#2e7d32;">Старт игры</button>
                </div>

                <h4>Участники:</h4>
                <div id="mafia-players-list" style="border:1px solid #333; padding:8px; border-radius:8px; max-height:160px; overflow-y:auto;"></div>

                <h4>События:</h4>
                <div id="mafia-logs-box" style="background:#151515; border:1px solid #333; padding:8px; border-radius:8px; font-size:12px; height:100px; overflow-y:auto; color:#aaa;"></div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let currentUser = "";
        let currentTarget = "#general";
        let mySecretRole = null;
        let isMicActive = false;

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
                socket.emit('get_channels');
                socket.emit('get_users');
            }
        });

        function toggleSidebar() { document.getElementById('sidebar').classList.toggle('hidden'); }

        function sendMsg() {
            const txt = document.getElementById('msg-input').value.trim();
            if (!txt) return;
            socket.emit('send_msg', { sender: currentUser, target: currentTarget, msg: txt });
            document.getElementById('msg-input').value = '';
        }

        socket.on('new_msg', (data) => {
            const box = document.getElementById('messages');
            const div = document.createElement('div');
            div.className = 'message ' + (data.sender === currentUser ? 'msg-me' : 'msg-other');
            div.innerText = data.sender + ': ' + data.msg;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        });

        /* ГОЛОС И МАФИЯ */
        function openMafiaModal() {
            document.getElementById('mafia-modal').style.display = 'flex';
            socket.emit('mafia_get_state');
        }
        function closeMafiaModal() { document.getElementById('mafia-modal').style.display = 'none'; }

        function joinMafiaGame() { socket.emit('mafia_join', { user: currentUser }); }
        function startMafiaGame() { socket.emit('mafia_start', { user: currentUser }); }

        function toggleRoleShow() {
            document.getElementById('my-role-badge').innerText = mySecretRole ? mySecretRole : 'Скрыта';
        }

        function toggleMic() {
            isMicActive = !isMicActive;
            document.getElementById('mic-btn').innerText = isMicActive ? "Выкл Микрофон" : "Вкл Микрофон";
            document.getElementById('voice-indicator').innerText = isMicActive ? "В эфире 🎙️" : "Готов";
        }

        socket.on('mafia_update', (data) => {
            document.getElementById('mafia-state-text').innerText = data.state.toUpperCase();
            
            if (data.players[currentUser]) {
                mySecretRole = data.players[currentUser].role;
                const micAllowed = data.players[currentUser].mic;
                
                const micBtn = document.getElementById('mic-btn');
                const voiceInd = document.getElementById('voice-indicator');
                
                if (micAllowed) {
                    micBtn.disabled = false;
                    voiceInd.innerText = isMicActive ? "В эфире 🎙️" : "Готов";
                    voiceInd.style.color = "#4caf50";
                } else {
                    micBtn.disabled = true;
                    isMicActive = false;
                    micBtn.innerText = "Вкл Микрофон";
                    voiceInd.innerText = "Заглушен (Ночь / Выбыл)";
                    voiceInd.style.color = "#ff5555";
                }
            }

            const plist = document.getElementById('mafia-players-list');
            plist.innerHTML = '';
            for (const [uname, pinfo] of Object.entries(data.players)) {
                const row = document.createElement('div');
                row.className = 'player-row';
                let act = '';
                if (pinfo.alive && uname !== currentUser) {
                    if (data.state === 'night' && mySecretRole === 'mafia') act = `<button class="act-btn" onclick="socket.emit('mafia_action', {user:'${currentUser}', target:'${uname}', action:'kill'})">Убить</button>`;
                    if (data.state === 'day') act = `<button class="act-btn" style="background:#f57c00;" onclick="socket.emit('mafia_action', {user:'${currentUser}', target:'${uname}', action:'vote'})">Голос</button>`;
                }
                row.innerHTML = `<span>${uname} ${pinfo.alive ? '🟢' : '💀'} ${pinfo.mic ? '🎙️' : '🔇'}</span> ${act}`;
                plist.appendChild(row);
            }

            const lbox = document.getElementById('mafia-logs-box');
            lbox.innerHTML = data.logs.join('<br>');
            lbox.scrollTop = lbox.scrollHeight;
        });

        socket.on('channels_list', (data) => {
            const box = document.getElementById('channels-list');
            box.innerHTML = '<div class="chat-item">#general</div>';
        });

        socket.on('users_list', (data) => {
            const box = document.getElementById('users-list');
            box.innerHTML = '';
            data.users.forEach(u => {
                if(u.username !== currentUser) box.innerHTML += `<div class="chat-item">👤 ${u.username}</div>`;
            });
        });
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
    if not username or not password: return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt, role FROM users WHERE username = ?", (username,))
    row = cur.fetchone()

    if row:
        pwd_hash, salt, role = row
        conn.close()
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
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
def on_get_channels():
    emit('channels_list', {'channels': ['#general']}, broadcast=True)

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
    emit('new_msg', data, broadcast=True)

# СОБЫТИЯ МАФИИ
def broadcast_mafia_state():
    emit('mafia_update', {
        'state': mafia_game.state,
        'players': mafia_game.players,
        'logs': mafia_game.logs
    }, broadcast=True)

@socketio.on('mafia_get_state')
def on_mafia_get_state():
    broadcast_mafia_state()

@socketio.on('mafia_join')
def on_mafia_join(data):
    if mafia_game.add_player(data['user']):
        mafia_game.logs.append(f"Игрок **{data['user']}** вошел в комнату.")
        broadcast_mafia_state()

@socketio.on('mafia_start')
def on_mafia_start(data):
    ok, msg = mafia_game.start_game()
    if ok: broadcast_mafia_state()

@socketio.on('mafia_action')
def on_mafia_action(data):
    user = data['user']
    target = data['target']
    act = data['action']

    if mafia_game.state == 'night':
        if act == 'kill' and mafia_game.players[user]['role'] == 'mafia':
            mafia_game.night_actions['mafia'][user] = target

        alive_mafia = [u for u, p in mafia_game.players.items() if p['alive'] and p['role'] == 'mafia']
        if len(mafia_game.night_actions['mafia']) >= len(alive_mafia):
            mafia_game.process_night()
            broadcast_mafia_state()

    elif mafia_game.state == 'day':
        if act == 'vote':
            mafia_game.day_votes[user] = target
            alive_count = sum(1 for p in mafia_game.players.values() if p['alive'])
            if len(mafia_game.day_votes) >= alive_count:
                mafia_game.process_day()
                broadcast_mafia_state()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
