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
                forward_from VARCHAR(255) DEFAULT '',
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
                members TEXT DEFAULT '',
                pinned_msg TEXT DEFAULT ''
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
                forward_from TEXT DEFAULT '',
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
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: sans-serif; }
        html, body { width: 100vw; height: 100dvh; overflow: hidden; background: var(--bg-color); color: var(--text-color); }
        
        #auth-screen { display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100dvh; padding: 20px; }
        .auth-box { background: var(--panel-bg); padding: 25px; border-radius: 16px; width: 100%; max-width: 340px; text-align: center; border: 1px solid var(--panel-border); }
        .auth-box h2 { margin-bottom: 15px; color: #3B8ED0; }
        input[type="text"], input[type="password"] { width: 100%; padding: 12px; margin: 8px 0; border-radius: 10px; border: 1px solid var(--panel-border); background: var(--bg-color); color: var(--text-color); outline: none; font-size: 16px; }
        button { width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none; background: var(--accent-color); color: #fff; font-weight: bold; cursor: pointer; font-size: 15px; }
        
        #app-screen { display: none; height: 100dvh; width: 100vw; position: relative; overflow: hidden; }
        #sidebar { width: 280px; background: var(--panel-bg); border-right: 2px solid var(--panel-border); display: flex; flex-direction: column; flex-shrink: 0; height: 100dvh; }
        .sidebar-header { padding: 12px 15px; background: var(--panel-bg); border-bottom: 1px solid var(--panel-border); font-weight: bold; display: flex; justify-content: space-between; align-items: center; }
        .chat-list { flex: 1; overflow-y: auto; padding: 10px; }
        .section-title { font-size: 11px; text-transform: uppercase; color: #888; margin: 12px 8px 6px 8px; font-weight: bold; }
        .chat-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; display: flex; align-items: center; gap: 10px; font-size: 14px; }
        .chat-item:hover, .chat-item.active { background: var(--item-hover); }
        .avatar { width: 34px; height: 34px; border-radius: 50%; background: #3B8ED0; display: flex; align-items: center; justify-content: center; font-weight: bold; color: #fff; flex-shrink: 0; }
        
        #chat-main { flex: 1; display: flex; flex-direction: column; background: var(--bg-color); height: 100dvh; }
        header { height: 56px; background: var(--panel-bg); padding: 0 15px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--panel-border); }
        .chat-container { flex: 1; overflow-y: auto; padding: 15px; display: flex; flex-direction: column; gap: 10px; }
        .message { max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 14px; display: flex; flex-direction: column; gap: 3px; word-wrap: break-word; }
        .msg-me { background: var(--accent-color); color: #fff; align-self: flex-end; }
        .msg-other { background: var(--panel-bg); border: 1px solid var(--panel-border); align-self: flex-start; }
        .msg-header { font-size: 11px; font-weight: bold; color: #82b1ff; }
        
        footer { height: 60px; background: var(--panel-bg); padding: 10px; display: flex; gap: 8px; border-top: 1px solid var(--panel-border); align-items: center; }
        footer input[type="text"] { flex: 1; height: 40px; background: var(--bg-color); color: var(--text-color); border: 1px solid var(--panel-border); border-radius: 10px; padding: 0 12px; font-size: 15px; outline: none; }
        footer button { width: 50px; height: 40px; margin: 0; }
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
                <span id="user-display">...</span>
                <button onclick="logout()" style="width:auto; padding:4px 8px; font-size:12px; background:#444;">Выйти</button>
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
                <h3 id="chat-title">#general</h3>
            </header>
            <div class="chat-container" id="messages"></div>
            <footer>
                <input type="text" id="msg-input" placeholder="Сообщение..." onkeypress="if(event.key==='Enter') sendMsg()">
                <button onclick="sendMsg()">></button>
            </footer>
        </div>
    </div>

    <script>
        const socket = io();
        let currentUser = "";
        let currentTarget = "#general";

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
                switchChat('#general');
            } else {
                document.getElementById('status').innerText = data.message;
            }
        });

        function switchChat(target) {
            currentTarget = target;
            document.getElementById('chat-title').innerText = target;
            document.getElementById('messages').innerHTML = '';
            socket.emit('load_history', { user: currentUser, target: currentTarget });
        }

        socket.on('channels_list', (data) => {
            const list = document.getElementById('channels-list');
            list.innerHTML = '';
            data.channels.forEach(ch => {
                const div = document.createElement('div');
                div.className = 'chat-item' + (ch.name === currentTarget ? ' active' : '');
                div.innerText = ch.name;
                div.onclick = () => switchChat(ch.name);
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
                div.innerText = u.username;
                div.onclick = () => switchChat(u.username);
                list.appendChild(div);
            });
        });

        function sendMsg() {
            const txt = document.getElementById('msg-input').value.trim();
            if (!txt) return;
            socket.emit('send_msg', { sender: currentUser, target: currentTarget, msg: txt });
            document.getElementById('msg-input').value = '';
        }

        socket.on('new_msg', (data) => {
            if (data.target === currentTarget || data.sender === currentTarget || (data.target.startsWith('#') && data.target === currentTarget)) {
                appendMessage(data);
            }
        });

        socket.on('history_loaded', (data) => {
            document.getElementById('messages').innerHTML = '';
            data.messages.forEach(msg => appendMessage(msg));
        });

        function appendMessage(msg) {
            const box = document.getElementById('messages');
            const isMe = msg.sender === currentUser;
            const div = document.createElement('div');
            div.className = 'message ' + (isMe ? 'msg-me' : 'msg-other');
            div.innerHTML = `<div class="msg-header">${isMe ? 'Вы' : msg.sender}</div><div>${msg.msg}</div>`;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }
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
        cur.execute("SELECT password_hash, salt, role FROM users WHERE username = %s", (username,))
    else:
        cur.execute("SELECT password_hash, salt, role FROM users WHERE username = ?", (username,))
        
    row = cur.fetchone()

    if row:
        pwd_hash, salt, role = row
        check_hash, _ = hash_password(password, salt)
        if check_hash == pwd_hash:
            conn.close()
            join_room(username)
            join_room('#general')
            emit('auth_response', {'status': 'ok', 'username': username, 'role': role})
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
        emit('auth_response', {'status': 'error', 'message': 'Успешно! Теперь нажмите Войти.'})
    except Exception:
        emit('auth_response', {'status': 'error', 'message': 'Логин уже занят'})
    finally: conn.close()

@socketio.on('get_channels')
def on_get_channels():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM channels")
    chs = [{'name': r[0]} for r in cur.fetchall()]
    conn.close()
    emit('channels_list', {'channels': chs})

@socketio.on('get_users')
def on_get_users():
    conn, db_type = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users")
    users = [{'username': r[0]} for r in cur.fetchall()]
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('load_history')
def on_load_history(data):
    user = data['user']
    target = data['target']
    conn, db_type = get_db()
    cur = conn.cursor()
    
    if target.startswith('#'):
        join_room(target)
        if db_type == 'postgres':
            cur.execute("SELECT sender, target, message FROM messages WHERE target = %s ORDER BY id ASC LIMIT 100", (target,))
        else:
            cur.execute("SELECT sender, target, message FROM messages WHERE target = ? ORDER BY id ASC LIMIT 100", (target,))
    else:
        if db_type == 'postgres':
            cur.execute("SELECT sender, target, message FROM messages WHERE (sender = %s AND target = %s) OR (sender = %s AND target = %s) ORDER BY id ASC LIMIT 100", (user, target, target, user))
        else:
            cur.execute("SELECT sender, target, message FROM messages WHERE (sender = ? AND target = ?) OR (sender = ? AND target = ?) ORDER BY id ASC LIMIT 100", (user, target, target, user))
        
    rows = cur.fetchall()
    conn.close()
    msgs = [{'sender': r[0], 'target': r[1], 'msg': r[2]} for r in rows]
    emit('history_loaded', {'messages': msgs})

@socketio.on('send_msg')
def on_send_msg(data):
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("INSERT INTO messages (sender, target, message) VALUES (%s, %s, %s)", (data['sender'], data['target'], data['msg']))
    else:
        cur.execute("INSERT INTO messages (sender, target, message) VALUES (?, ?, ?)", (data['sender'], data['target'], data['msg']))
    conn.commit()
    conn.close()

    target = data['target']
    if target.startswith('#'): emit('new_msg', data, room=target)
    else:
        emit('new_msg', data, room=target)
        if data['sender'] != target: emit('new_msg', data, room=data['sender'])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
