import os
import time
import random
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from pywebpush import webpush, WebPushException
import sqlite3
import psycopg2

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_change_me'
socketio = SocketIO(app, cors_allowed_origins="*")

# Переменные окружения VAPID
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_CLAIMS = {"sub": "mailto:admin@example.com"}

# Глобальное состояние
online_users = {}       # username -> request.sid
user_sockets = {}       # sid -> username
push_subscriptions = {} # username -> subscription_json
active_calls = {}       # roomId -> call_data
channels = [{'name': '#general'}]
mafia_rooms = {}        # room_name -> room_data

def get_db():
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        conn = psycopg2.connect(database_url, sslmode='require')
        return conn, 'postgres'
    else:
        conn = sqlite3.connect('database.db')
        return conn, 'sqlite'

def init_db():
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user',
            avatar TEXT DEFAULT '',
            last_seen DOUBLE PRECISION DEFAULT 0
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            sender TEXT,
            target TEXT,
            message TEXT,
            msg_type TEXT DEFAULT 'text',
            ttl INTEGER DEFAULT 0,
            reply_to INTEGER DEFAULT NULL,
            reply_text TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            is_edited INTEGER DEFAULT 0,
            reactions TEXT DEFAULT '{}'
        )''')
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password TEXT;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION DEFAULT 0;")
        except Exception:
            pass
    else:
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user',
            avatar TEXT DEFAULT '',
            last_seen REAL DEFAULT 0
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            target TEXT,
            message TEXT,
            msg_type TEXT DEFAULT 'text',
            ttl INTEGER DEFAULT 0,
            reply_to INTEGER DEFAULT NULL,
            reply_text TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            is_edited INTEGER DEFAULT 0,
            reactions TEXT DEFAULT '{}'
        )''')
    conn.commit()
    conn.close()

init_db()

def send_web_push(target_user, title, body, room_id=""):
    sub = push_subscriptions.get(target_user)
    if not sub or not VAPID_PRIVATE_KEY:
        return
    try:
        webpush(
            subscription_info=sub,
            data=f'{{"title": "{title}", "body": "{body}", "roomId": "{room_id}"}}',
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
    except WebPushException as ex:
        print(f"Push error for {target_user}:", ex)

# --- АВТОРИЗАЦИЯ И РЕГИСТРАЦИЯ ---

@socketio.on('login')
def on_login(data):
    u = str(data.get('username', '')).strip()
    p = str(data.get('password', '')).strip()
    if not u or not p:
        emit('auth_response', {'status': 'error', 'message': 'Заполните логин и пароль'})
        return
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        if db_type == 'postgres':
            cur.execute("SELECT password, role, avatar FROM users WHERE LOWER(username)=LOWER(%s)", (u,))
        else:
            cur.execute("SELECT password, role, avatar FROM users WHERE LOWER(username)=LOWER(?)", (u,))
        user = cur.fetchone()
        conn.close()

        if user and str(user[0]).strip() == p:
            online_users[u] = request.sid
            user_sockets[request.sid] = u
            join_room(u)
            emit('auth_response', {'status': 'ok', 'username': u, 'role': user[1] or 'user', 'avatar': user[2] or ''})
            broadcast_users()
        else:
            emit('auth_response', {'status': 'error', 'message': 'Неверный логин или пароль'})
    except Exception as e:
        if conn: conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Ошибка авторизации на сервере'})

@socketio.on('register')
def on_register(data):
    u = str(data.get('username', '')).strip()
    p = str(data.get('password', '')).strip()
    if not u or not p:
        emit('auth_response', {'status': 'error', 'message': 'Введите логин и пароль'})
        return
    role = 'admin' if u.lower() == 'totr' else 'user'
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        if db_type == 'postgres':
            cur.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(%s)", (u,))
        else:
            cur.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?)", (u,))
        existing = cur.fetchone()
        if existing:
            if db_type == 'postgres':
                cur.execute("UPDATE users SET password=%s, role=%s WHERE id=%s", (p, role, existing[0]))
            else:
                cur.execute("UPDATE users SET password=?, role=? WHERE id=?", (p, role, existing[0]))
        else:
            if db_type == 'postgres':
                cur.execute("INSERT INTO users (username, password, role) VALUES (%s, %s, %s)", (u, p, role))
            else:
                cur.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (u, p, role))
        conn.commit()
        conn.close()
        online_users[u] = request.sid
        user_sockets[request.sid] = u
        join_room(u)
        emit('auth_response', {'status': 'ok', 'username': u, 'role': role, 'avatar': ''})
        broadcast_users()
    except Exception as e:
        if conn: conn.close()
        emit('auth_response', {'status': 'error', 'message': 'Ошибка регистрации'})

@socketio.on('disconnect')
def on_disconnect():
    u = user_sockets.get(request.sid)
    if u:
        if u in online_users: del online_users[u]
        del user_sockets[request.sid]
        conn, db_type = get_db()
        cur = conn.cursor()
        now = time.time()
        try:
            if db_type == 'postgres':
                cur.execute("UPDATE users SET last_seen=%s WHERE username=%s", (now, u))
            else:
                cur.execute("UPDATE users SET last_seen=? WHERE username=?", (now, u))
            conn.commit()
        except Exception:
            pass
        conn.close()
        broadcast_users()

def broadcast_users():
    conn, db_type = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT username, avatar, last_seen FROM users")
        users = [{'username': r[0], 'avatar': r[1] or '', 'online': r[0] in online_users, 'last_seen': r[2] or 0} for r in cur.fetchall()]
    except Exception:
        users = []
    conn.close()
    emit('users_list', {'users': users}, broadcast=True)

@socketio.on('get_users')
def on_get_users():
    broadcast_users()

@socketio.on('get_channels')
def on_get_channels():
    emit('channels_list', {'channels': channels})

@socketio.on('create_channel')
def on_create_channel(data):
    name = data.get('name')
    if name and not any(c['name'] == name for c in channels):
        channels.append({'name': name})
        emit('channels_list', {'channels': channels}, broadcast=True)

# --- СООБЩЕНИЯ И ИСТОРИЯ ---

@socketio.on('load_history')
def on_load_history(data):
    u = data.get('user')
    target = data.get('target')
    conn, db_type = get_db()
    cur = conn.cursor()
    if target.startswith('#'):
        if db_type == 'postgres':
            cur.execute("SELECT id, sender, target, message, msg_type, ttl, reply_to, reply_text, is_read, is_edited, reactions FROM messages WHERE target=%s ORDER BY id ASC", (target,))
        else:
            cur.execute("SELECT id, sender, target, message, msg_type, ttl, reply_to, reply_text, is_read, is_edited, reactions FROM messages WHERE target=? ORDER BY id ASC", (target,))
    else:
        if db_type == 'postgres':
            cur.execute("SELECT id, sender, target, message, msg_type, ttl, reply_to, reply_text, is_read, is_edited, reactions FROM messages WHERE (sender=%s AND target=%s) OR (sender=%s AND target=%s) ORDER BY id ASC", (u, target, target, u))
        else:
            cur.execute("SELECT id, sender, target, message, msg_type, ttl, reply_to, reply_text, is_read, is_edited, reactions FROM messages WHERE (sender=? AND target=?) OR (sender=? AND target=?) ORDER BY id ASC", (u, target, target, u))
    rows = cur.fetchall()
    conn.close()
    msgs = [{
        'id': r[0], 'sender': r[1], 'target': r[2], 'msg': r[3], 'msg_type': r[4],
        'ttl': r[5], 'reply_to': r[6], 'reply_text': r[7], 'is_read': r[8],
        'is_edited': r[9], 'reactions': r[10]
    } for r in rows]
    emit('history_loaded', {'messages': msgs})

@socketio.on('send_msg')
def on_send_msg(data):
    s = data.get('sender')
    t = data.get('target')
    m = data.get('msg')
    mt = data.get('msg_type', 'text')
    ttl = data.get('ttl', 0)
    rt = data.get('reply_to')
    rtx = data.get('reply_text', '')

    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("INSERT INTO messages (sender, target, message, msg_type, ttl, reply_to, reply_text) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", (s, t, m, mt, ttl, rt, rtx))
        msg_id = cur.fetchone()[0]
    else:
        cur.execute("INSERT INTO messages (sender, target, message, msg_type, ttl, reply_to, reply_text) VALUES (?, ?, ?, ?, ?, ?, ?)", (s, t, m, mt, ttl, rt, rtx))
        msg_id = cur.lastrowid
    conn.commit()
    conn.close()

    msg_data = {
        'id': msg_id, 'sender': s, 'target': t, 'msg': m, 'msg_type': mt,
        'ttl': ttl, 'reply_to': rt, 'reply_text': rtx, 'is_read': 0,
        'is_edited': 0, 'reactions': '{}'
    }

    if t.startswith('#'):
        emit('new_msg', msg_data, broadcast=True)
    else:
        emit('new_msg', msg_data, room=t)
        emit('new_msg', msg_data, room=s)

@socketio.on('mark_read')
def on_mark_read(data):
    u = data.get('user')
    t = data.get('target')
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE messages SET is_read=1 WHERE sender=%s AND target=%s", (t, u))
    else:
        cur.execute("UPDATE messages SET is_read=1 WHERE sender=? AND target=?", (t, u))
    conn.commit()
    conn.close()
    emit('messages_read', {'user': u, 'target': t}, room=t)

@socketio.on('delete_msg')
def on_delete_msg(data):
    mid = data.get('msg_id')
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("DELETE FROM messages WHERE id=%s", (mid,))
    else:
        cur.execute("DELETE FROM messages WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    emit('msg_deleted', {'msg_id': mid}, broadcast=True)

@socketio.on('edit_msg')
def on_edit_msg(data):
    mid = data.get('msg_id')
    txt = data.get('new_text')
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE messages SET message=%s, is_edited=1 WHERE id=%s", (txt, mid))
    else:
        cur.execute("UPDATE messages SET message=?, is_edited=1 WHERE id=?", (txt, mid))
    conn.commit()
    conn.close()
    emit('msg_edited', {'msg_id': mid, 'new_text': txt}, broadcast=True)

@socketio.on('update_avatar')
def on_update_avatar(data):
    u = data.get('user')
    av = data.get('avatar')
    conn, db_type = get_db()
    cur = conn.cursor()
    if db_type == 'postgres':
        cur.execute("UPDATE users SET avatar=%s WHERE username=%s", (av, u))
    else:
        cur.execute("UPDATE users SET avatar=? WHERE username=?", (av, u))
    conn.commit()
    conn.close()
    emit('avatar_updated', {'user': u, 'avatar': av}, broadcast=True)

@socketio.on('user_typing')
def on_user_typing(data):
    emit('display_typing', data, room=data.get('target'))

# --- ЛОГИКА ИГРЫ МАФИЯ ---

@socketio.on('mafia_get_rooms')
def on_mafia_get_rooms():
    rooms_summary = [{'name': name, 'count': len(room['players']), 'started': room['started']} for name, room in mafia_rooms.items()]
    emit('mafia_rooms_list', {'rooms': rooms_summary})

@socketio.on('mafia_create_room')
def on_mafia_create_room(data):
    rname = data.get('room_name')
    user = data.get('user')
    if rname and rname not in mafia_rooms:
        mafia_rooms[rname] = {
            'creator': user,
            'players': [user],
            'roles': {},
            'started': False,
            'phase': 'lobby'
        }
        join_room('mafia_' + rname)
        emit('mafia_room_joined', {'room_name': rname, 'players': [user], 'is_creator': True})
        on_mafia_get_rooms()

@socketio.on('mafia_join_room')
def on_mafia_join_room(data):
    rname = data.get('room_name')
    user = data.get('user')
    if rname in mafia_rooms and not mafia_rooms[rname]['started']:
        if user not in mafia_rooms[rname]['players']:
            mafia_rooms[rname]['players'].append(user)
        join_room('mafia_' + rname)
        is_creator = (mafia_rooms[rname]['creator'] == user)
        emit('mafia_room_joined', {'room_name': rname, 'players': mafia_rooms[rname]['players'], 'is_creator': is_creator}, room=request.sid)
        emit('mafia_update_players', {'players': mafia_rooms[rname]['players']}, room='mafia_' + rname)
        on_mafia_get_rooms()

@socketio.on('mafia_start_game')
def on_mafia_start_game(data):
    rname = data.get('room_name')
    if rname in mafia_rooms and len(mafia_rooms[rname]['players']) >= 3:
        room = mafia_rooms[rname]
        room['started'] = True
        players = list(room['players'])
        random.shuffle(players)
        
        roles = {}
        roles[players[0]] = 'Мафия'
        roles[players[1]] = 'Детектив'
        for p in players[2:]:
            roles[p] = 'Мирный житель'
        
        room['roles'] = roles
        for p in players:
            sid = online_users.get(p)
            if sid:
                emit('mafia_game_started', {'role': roles[p]}, room=sid)

# --- МОДУЛЬ ЗВОНКОВ (WEBRTC И PUSH) ---

@socketio.on('call_start')
def on_call_start(data):
    caller = data.get('caller')
    target = data.get('target')
    call_type = data.get('call_type', 'audio')
    
    sorted_pair = sorted([caller, target])
    room_id = f"call_{sorted_pair[0]}_{sorted_pair[1]}"

    active_calls[room_id] = {
        'caller': caller,
        'target': target,
        'call_type': call_type,
        'offer': data.get('offer'),
        'roomId': room_id
    }

    call_title = "📹 Входящий видеозвонок" if call_type == 'video' else "📞 Входящий звонок"
    send_web_push(target, f"{call_title} от @{caller}", "Нажмите, чтобы ответить", room_id)

    call_msg = {
        'id': int(time.time() * 1000),
        'sender': caller,
        'target': target,
        'msg': f"{call_title}...",
        'msg_type': 'call_card',
        'roomId': room_id,
        'call_type': call_type,
        'is_read': 0,
        'is_edited': 0
    }

    emit('new_msg', call_msg, room=target)
    emit('new_msg', call_msg, room=caller)

    if target in online_users:
        emit('incoming_call', active_calls[room_id], room=target)

@socketio.on('check_active_call')
def on_check_active_call(data):
    user = data.get('user')
    for rid, call_data in active_calls.items():
        if call_data['target'] == user:
            emit('incoming_call', call_data, room=request.sid)
            break

@socketio.on('call_accept')
def on_call_accept(data):
    target = data.get('target')
    emit('call_accepted', data, room=target)

@socketio.on('call_end')
def on_call_end(data):
    target = data.get('target')
    caller = data.get('caller')
    room_id = data.get('roomId')

    if room_id in active_calls:
        del active_calls[room_id]

    if target: emit('call_ended', data, room=target)
    if caller: emit('call_ended', data, room=caller)

@socketio.on('call_signal')
def on_call_signal(data):
    target = data.get('target')
    if target in online_users:
        emit('call_signal', data, room=target)

@socketio.on('join_call')
def on_join_call(data):
    room_id = data.get('roomId')
    join_room(room_id)
    emit('user_joined_call', {'sid': request.sid}, room=room_id, include_self=False)

@socketio.on('leave_call')
def on_leave_call(data):
    room_id = data.get('roomId')
    leave_room(room_id)
    emit('user_left_call', {'sid': request.sid}, room=room_id, include_self=False)

@socketio.on('signal_to_peer')
def on_signal_to_peer(data):
    target_sid = data.get('targetSid')
    emit('signal_from_peer', {'senderSid': request.sid, 'signal': data.get('signal')}, room=target_sid)

@socketio.on('save_push_subscription')
def on_save_push(data):
    user = data.get('user')
    sub = data.get('subscription')
    if user and sub:
        push_subscriptions[user] = sub

@app.route('/')
def index():
    return render_template('index.html', vapid_public_key=VAPID_PUBLIC_KEY)

@app.route('/sw.js')
def sw():
    return send_from_directory('static', 'sw.js')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
