const socket = io();

let currentRoomId = "default-room"; // ID чата/комнаты
let localStream = null;
let isAudioMuted = false;
let isVideoOff = false;

// Хранилище всех P2P соединений: { socketSid: RTCPeerConnection }
const peerConnections = {};

// STUN-серверы Google для проброса WebRTC
const rtcConfig = {
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'stun:stun1.l.google.com:19302' }
  ]
};

// ==========================================
// 1. ИНИЦИАЛИЗАЦИЯ И УПРАВЛЕНИЕ МЕДИА
// ==========================================

async function startGroupCall(roomId = "default-room") {
  currentRoomId = roomId;
  document.getElementById('call-screen').classList.add('active');

  try {
    // Запрашиваем доступ к камере и микрофону
    localStream = await navigator.mediaDevices.getUserMedia({
      video: true,
      audio: true
    });
    
    document.getElementById('local-video').srcObject = localStream;

    // Входим в комнату звонка на бэкенде
    socket.emit('join_call', { roomId: currentRoomId });

  } catch (err) {
    console.error("Ошибка получения доступа к медиаустройствам:", err);
    alert("Не удалось получить доступ к камере или микрофону.");
  }
}

// Переключение микрофона
function toggleMicrophone() {
  if (!localStream) return;
  const audioTrack = localStream.getAudioTracks()[0];
  if (audioTrack) {
    isAudioMuted = !isAudioMuted;
    audioTrack.enabled = !isAudioMuted;
    
    const btn = document.getElementById('btn-toggle-mic');
    const label = document.getElementById('mic-label');
    btn.classList.toggle('disabled', isAudioMuted);
    label.innerText = isAudioMuted ? 'Unmute' : 'Mute';
  }
}

// Переключение камеры
function toggleCamera() {
  if (!localStream) return;
  const videoTrack = localStream.getVideoTracks()[0];
  if (videoTrack) {
    isVideoOff = !isVideoOff;
    videoTrack.enabled = !isVideoOff;
    
    const btn = document.getElementById('btn-toggle-cam');
    const label = document.getElementById('cam-label');
    btn.classList.toggle('disabled', isVideoOff);
    label.innerText = isVideoOff ? 'Cam On' : 'Cam Off';
  }
}

// Выход из звонка
function leaveGroupCall() {
  socket.emit('leave_call', { roomId: currentRoomId });

  // Закрываем все P2P соединения
  Object.keys(peerConnections).forEach(sid => {
    peerConnections[sid].close();
    delete peerConnections[sid];
  });

  // Останавливаем треки локальной камеры и микрофона
  if (localStream) {
    localStream.getTracks().forEach(track => track.stop());
    localStream = null;
  }

  // Очищаем DOM сетки кроме локального видео
  const grid = document.getElementById('video-grid');
  const remoteContainers = grid.querySelectorAll('.video-container:not(#local-video-container)');
  remoteContainers.forEach(el => el.remove());

  document.getElementById('call-screen').classList.remove('active');
}

// ==========================================
// 2. СЕТЕВАЯ ЛОГИКА И СИГНАЛИНГ WEBRTC
// ==========================================

// Сервер прислал список участников, которые уже в комнате
socket.on('current_room_users', async ({ users }) => {
  for (const sid of users) {
    const pc = createPeerConnection(sid);
    peerConnections[sid] = pc;

    // Добавляем наши локальные медиатреки в PeerConnection
    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

    // Создаем Offer для подключения к существующему участнику
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    socket.emit('signal_to_peer', {
      targetSid: sid,
      signal: { sdp: pc.localDescription }
    });
  }
});

// К комнате подключился новый пользователь
socket.on('user_joined_call', ({ sid }) => {
  // Мы просто ждем от него Offer, создав заготовку connection
  const pc = createPeerConnection(sid);
  peerConnections[sid] = pc;
  localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
});

// Обработка входящих сигналов (Offer / Answer / ICE-candidates)
socket.on('signal_from_peer', async ({ senderSid, signal }) => {
  let pc = peerConnections[senderSid];

  if (!pc) {
    pc = createPeerConnection(senderSid);
    peerConnections[senderSid] = pc;
    if (localStream) {
      localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
    }
  }

  if (signal.sdp) {
    await pc.setRemoteDescription(new RTCSessionDescription(signal.sdp));

    // Если получили Offer — генерируем Answer
    if (signal.sdp.type === 'offer') {
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      
      socket.emit('signal_to_peer', {
        targetSid: senderSid,
        signal: { sdp: pc.localDescription }
      });
    }
  } else if (signal.candidate) {
    await pc.addIceCandidate(new RTCIceCandidate(signal.candidate));
  }
});

// Построение PeerConnection для конкретного участника
function createPeerConnection(sid) {
  const pc = new RTCPeerConnection(rtcConfig);

  // Отправка своих ICE кандидатов партнеру
  pc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit('signal_to_peer', {
        targetSid: sid,
        signal: { candidate: event.candidate }
      });
    }
  };

  // Отрисовка полученного видеопотока
  pc.ontrack = (event) => {
    addRemoteVideoTrack(sid, event.streams[0]);
  };

  return pc;
}

// Добавление карточки нового участника в интерфейс
function addRemoteVideoTrack(sid, stream) {
  let container = document.getElementById(`video-container-${sid}`);
  
  if (!container) {
    container = document.createElement('div');
    container.id = `video-container-${sid}`;
    container.className = 'video-container';

    const video = document.createElement('video');
    video.id = `video-${sid}`;
    video.autoplay = true;
    video.playsInline = true;

    const label = document.createElement('span');
    label.className = 'user-label';
    label.innerText = `User ${sid.slice(0, 4)}`;

    container.appendChild(video);
    container.appendChild(label);
    document.getElementById('video-grid').appendChild(container);
  }

  const remoteVideo = document.getElementById(`video-${sid}`);
  remoteVideo.srcObject = stream;
}

// Когда участник покинул звонок
socket.on('user_left_call', ({ sid }) => {
  if (peerConnections[sid]) {
    peerConnections[sid].close();
    delete peerConnections[sid];
  }

  const container = document.getElementById(`video-container-${sid}`);
  if (container) {
    container.remove();
  }
});

// Автостарт (для быстрых тестов)
// window.onload = () => startGroupCall("test-room");
