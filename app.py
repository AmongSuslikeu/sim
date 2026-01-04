# ...existing code...
import math
import time
import threading
import logging
import random

from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO

# ============================================================
#                   SECURE VOXEL CSG FPS - ONE FILE
#  Features: movement, jumping, visible revolver, voice commands
#            voice chat broadcasting, normal chat, player list with
#            status (dead/alive/in fight), neon UI + pointer-lock toggle
# ============================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'csg_secure_full'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# --- Game constants ---
TICK_RATE = 0.05
PLAYER_SPEED = 4.0          # world units / second
PLAYER_RADIUS = 0.3
PLAYER_HEIGHT = 1.6
GRAVITY = 18.0
JUMP_VELOCITY = 6.5
RESPAWN_TIME = 3.0
MAX_INTERACT_DISTANCE = 30.0
IN_FIGHT_TTL = 4.0

# --- World ---
world = {}
for x in range(-10, 10):
    for z in range(-10, 10):
        if x == -10 or x == 9 or z == -10 or z == 9:
            world[(x, 0, z)] = 1
for y in range(0, 3):
    world[(0, y, 3)] = 1
    world[(3, y, 0)] = 1
    world[(-3, y, 0)] = 1

players = {}
world_version = 0
world_changed_blocks = {}
state_lock = threading.Lock()

def mark_world_change(pos, val):
    global world_version
    world_version += 1
    world_changed_blocks[pos] = val

def add_block(x,y,z):
    if (x,y,z) not in world:
        world[(x,y,z)] = 1
        mark_world_change((x,y,z), 1)

def remove_block(x,y,z):
    if (x,y,z) in world:
        del world[(x,y,z)]
        mark_world_change((x,y,z), 0)

def is_block_solid(x,y,z):
    return world.get((x,y,z)) == 1

def spawn_position():
    # randomized spawn to reduce stacking
    return [random.uniform(-2,2), PLAYER_HEIGHT + 0.01, random.uniform(-2,2)]

def reset_player(p):
    p['pos'] = spawn_position()
    p['yaw'] = 0.0
    p['pitch'] = 0.0
    p['hp'] = 100
    p['dead'] = False
    p['respawn_at'] = None
    p['vel'] = [0.0, 0.0, 0.0]
    p['on_ground'] = True
    p['in_fight_until'] = 0.0

def direction_from_yaw_pitch(yaw, pitch):
    cx = math.cos(pitch)
    dx = math.sin(yaw) * cx
    dy = math.sin(pitch)
    dz = -math.cos(yaw) * cx
    mag = math.sqrt(dx*dx + dy*dy + dz*dz)
    if mag == 0:
        return [0.0, 0.0, -1.0]
    return [dx/mag, dy/mag, dz/mag]

def move_player(p, dt):
    # dt in seconds
    keys = p.get('inputs', {})
    yaw = p.get('yaw', 0.0)
    px, py, pz = p['pos']
    vx, vy, vz = p.get('vel', [0,0,0])

    # horizontal movement vector in world-space
    forward_x = math.sin(yaw)
    forward_z = -math.cos(yaw)
    right_x = math.cos(yaw)
    right_z = math.sin(yaw)

    move_x = 0.0
    move_z = 0.0
    if keys.get('w'): move_x += forward_x; move_z += forward_z
    if keys.get('s'): move_x -= forward_x; move_z -= forward_z
    if keys.get('a'): move_x -= right_x;   move_z -= right_z
    if keys.get('d'): move_x += right_x;   move_z += right_z

    mag = math.sqrt(move_x*move_x + move_z*move_z)
    if mag > 0:
        move_x = move_x/mag * PLAYER_SPEED
        move_z = move_z/mag * PLAYER_SPEED

    # apply to velocity
    vx = move_x
    vz = move_z

    # jumping
    if keys.get('jump') and p.get('on_ground', False):
        vy = JUMP_VELOCITY
        p['on_ground'] = False

    # gravity
    vy -= GRAVITY * dt

    # integrate
    new_x = px + vx * dt
    new_y = py + vy * dt
    new_z = pz + vz * dt

    # simple ground collision (world ground at y = 0)
    ground_y = PLAYER_HEIGHT
    if new_y <= ground_y:
        new_y = ground_y
        vy = 0.0
        p['on_ground'] = True

    # simple collision against solid blocks at integer positions (very basic)
    gx, gz = int(math.floor(new_x)), int(math.floor(new_z))
    gy = int(math.floor(new_y - 0.1))
    # if head or feet colliding, revert horizontal move
    if is_block_solid(gx, gy, gz):
        new_x = px
        new_z = pz

    p['pos'][0], p['pos'][1], p['pos'][2] = new_x, new_y, new_z
    p['vel'] = [vx, vy, vz]

def voxel_ray_cast(origin, direction, max_distance=30.0):
    ox, oy, oz = origin
    dx, dy, dz = direction
    x, y, z = math.floor(ox), math.floor(oy), math.floor(oz)

    step_x = 1 if dx > 0 else -1 if dx < 0 else 0
    step_y = 1 if dy > 0 else -1 if dy < 0 else 0
    step_z = 1 if dz > 0 else -1 if dz < 0 else 0

    def intbound(s, ds):
        if ds > 0:
            return (math.floor(s) + 1 - s) / ds
        elif ds < 0:
            return (s - math.floor(s)) / -ds
        else:
            return float('inf')

    t_max_x = intbound(ox, dx)
    t_max_y = intbound(oy, dy)
    t_max_z = intbound(oz, dz)

    t_delta_x = abs(1.0/dx) if dx != 0 else float('inf')
    t_delta_y = abs(1.0/dy) if dy != 0 else float('inf')
    t_delta_z = abs(1.0/dz) if dz != 0 else float('inf')

    t = 0.0
    while t <= max_distance:
        if is_block_solid(x, y, z):
            return (x, y, z)
        if t_max_x < t_max_y:
            if t_max_x < t_max_z:
                x += step_x
                t = t_max_x
                t_max_x += t_delta_x
            else:
                z += step_z
                t = t_max_z
                t_max_z += t_delta_z
        else:
            if t_max_y < t_max_z:
                y += step_y
                t = t_max_y
                t_max_y += t_delta_y
            else:
                z += step_z
                t = t_max_z
                t_max_z += t_delta_z
    return None

def raycast_players(origin, direction, ignore_sid, max_distance=30.0):
    ox, oy, oz = origin
    dx, dy, dz = direction
    closest_sid, closest_dist = None, max_distance
    for sid, p in players.items():
        if sid == ignore_sid or p.get('dead'):
            continue
        px, py, pz = p['pos']
        vx, vy, vz = px - ox, py - oy, pz - oz
        proj = vx*dx + vy*dy + vz*dz
        if proj < 0 or proj > max_distance:
            continue
        cx = ox + dx * proj
        cy = oy + dy * proj
        cz = oz + dz * proj
        dist2 = (px - cx)**2 + (py - cy)**2 + (pz - cz)**2
        if dist2 <= PLAYER_RADIUS * PLAYER_RADIUS:
            player_bottom = py - (PLAYER_HEIGHT - 0.3)
            player_top = py + 0.4
            if player_bottom <= cy <= player_top and proj < closest_dist:
                closest_sid, closest_dist = sid, proj
    if closest_sid:
        return (closest_sid, closest_dist)
    return (None, None)

# --- Server tick thread ---
def server_tick():
    last_time = time.time()
    while True:
        try:
            now = time.time()
            dt = now - last_time
            last_time = now
            with state_lock:
                # move players
                for sid, p in list(players.items()):
                    if p.get('dead'):
                        if p.get('respawn_at') and now >= p['respawn_at']:
                            reset_player(p)
                        else:
                            continue
                    move_player(p, dt)
                    # clear transient jump flag so single key press works
                    # in this implementation inputs persist client-side; server trusts it
                    # decay in-fight
                    if p.get('in_fight_until', 0) < now:
                        p['in_fight'] = False

                # prepare payload
                payload = {}
                for sid, p in players.items():
                    payload[sid] = {
                        'pos': p['pos'],
                        'yaw': p['yaw'],
                        'hp': p['hp'],
                        'dead': p['dead'],
                        'team': p.get('team', 'A'),
                        'in_fight': bool(p.get('in_fight', False))
                    }

                changed = [{'pos':[x,y,z], 'val':v} for (x,y,z), v in world_changed_blocks.items()]
                current_version = world_version
                world_changed_blocks.clear()

            socketio.emit('state', {
                'players': payload,
                'worldDiff': {'version': current_version, 'changes': changed}
            })
        except Exception:
            logging.exception("Exception in server_tick")
        socketio.sleep(TICK_RATE)

threading.Thread(target=server_tick, daemon=True).start()

# --- Socket handlers ---
@socketio.on('join')
def on_join():
    try:
        sid = request.sid
        with state_lock:
            team = random.choice(['A','B'])
            players[sid] = {
                'pos': spawn_position(),
                'yaw': 0.0,
                'pitch': 0.0,
                'hp': 100,
                'inputs': {},
                'dead': False,
                'respawn_at': None,
                'vel': [0,0,0],
                'on_ground': True,
                'team': team,
                'in_fight': False,
                'in_fight_until': 0.0
            }
            full_world = [{'pos':[x,y,z],'val':1} for (x,y,z) in world.keys()]
            socketio.emit('world_init', {'world': full_world}, room=sid)
            # announce join chat
            socketio.emit('chat', {'from':'SERVER', 'text': f'Player {sid[:6]} joined (team {team})'})
    except Exception:
        logging.exception("Error in join handler")

@socketio.on('disconnect')
def on_disconnect():
    try:
        sid = request.sid
        with state_lock:
            if sid in players:
                team = players[sid].get('team', '?')
                del players[sid]
            socketio.emit('chat', {'from':'SERVER', 'text': f'Player {sid[:6]} disconnected'})
    except Exception:
        logging.exception("Error in disconnect handler")

@socketio.on('input')
def on_input(data):
    try:
        sid = request.sid
        with state_lock:
            p = players.get(sid)
            if not p: return
            p['inputs'] = data.get('keys', {})
            p['yaw'] = float(data.get('yaw', 0.0))
            p['pitch'] = float(data.get('pitch', 0.0))
            # keep jump flag as boolean - client should set true only on keydown
            if data.get('jump'):
                p['inputs']['jump'] = True
    except Exception:
        logging.exception("Error in input handler")

@socketio.on('shoot')
def on_shoot(_data=None):
    try:
        sid = request.sid
        now = time.time()
        with state_lock:
            shooter = players.get(sid)
            if not shooter or shooter.get('dead'): return
            origin = shooter['pos'][:]
            origin[1] += 0.2
            yaw = shooter['yaw']; pitch = shooter['pitch']
            direction = direction_from_yaw_pitch(yaw, pitch)
            hit_sid, hit_dist = raycast_players(origin, direction, ignore_sid=sid)
            if hit_sid and hit_dist is not None and hit_dist <= MAX_INTERACT_DISTANCE:
                target = players.get(hit_sid)
                if target and not target.get('dead'):
                    target['hp'] -= 34
                    shooter['in_fight'] = True; target['in_fight'] = True
                    shooter['in_fight_until'] = now + IN_FIGHT_TTL
                    target['in_fight_until'] = now + IN_FIGHT_TTL
                    if target['hp'] <= 0:
                        target['hp'] = 0
                        target['dead'] = True
                        target['respawn_at'] = now + RESPAWN_TIME
                        socketio.emit('chat', {'from':'SERVER', 'text': f'Player {hit_sid[:6]} was de-voxeled by {sid[:6]}'})
                    socketio.emit('hit', {'target': hit_sid}, room=sid)
                return
            # block hit
            hit_block = voxel_ray_cast(origin, direction, max_distance=30.0)
            if hit_block:
                bx, by, bz = hit_block
                cx, cy, cz = bx+0.5, by+0.5, bz+0.5
                dx_, dy_, dz_ = cx-origin[0], cy-origin[1], cz-origin[2]
                dist = math.sqrt(dx_*dx_ + dy_*dy_ + dz_*dz_)
                if dist <= MAX_INTERACT_DISTANCE:
                    remove_block(bx,by,bz)
                    socketio.emit('block_break', {'pos':[bx,by,bz]}, room=sid)
    except Exception:
        logging.exception("Error in shoot handler")

@socketio.on('place')
def on_place():
    try:
        sid = request.sid
        with state_lock:
            p = players.get(sid)
            if not p or p.get('dead'): return
            direction = direction_from_yaw_pitch(p['yaw'], p['pitch'])
            x,y,z = p['pos']
            place_x = x + direction[0] * 2.0
            place_y = y + direction[1] * 2.0
            place_z = z + direction[2] * 2.0
            vx, vy, vz = math.floor(place_x), math.floor(place_y), math.floor(place_z)
            dx_, dy_, dz_ = (vx+0.5)-x, (vy+0.5)-y, (vz+0.5)-z
            dist = math.sqrt(dx_*dx_ + dy_*dy_ + dz_*dz_)
            if dist > MAX_INTERACT_DISTANCE: return
            if not is_block_solid(vx, vy, vz):
                add_block(vx, vy, vz)
    except Exception:
        logging.exception("Error in place handler")

@socketio.on('chat')
def on_chat(msg):
    try:
        sid = request.sid
        text = msg.get('text','')[:300]
        name = msg.get('from') or sid[:6]
        socketio.emit('chat', {'from': name, 'text': text})
    except Exception:
        logging.exception("Error in chat handler")

# Simple binary-free voice broadcast (dataURL chunks)
@socketio.on('voice')
def on_voice(data):
    try:
        sid = request.sid
        # data is expected as JSON object {'data': dataUrlString}
        payload = {'from': sid, 'data': data.get('data') if isinstance(data, dict) else None}
        # broadcast to all other clients
        socketio.emit('voice', payload, broadcast=True, include_self=False)
    except Exception:
        logging.exception("voice error")

# WebRTC signaling relay (kept for compatibility with optional WebRTC approach)
@socketio.on('webrtc_offer')
def on_webrtc_offer(data):
    try:
        sid = request.sid
        payload = {'from': sid, 'sdp': data.get('sdp')}
        # broadcast to others
        socketio.emit('webrtc_offer', payload, broadcast=True)
    except Exception:
        logging.exception("webrtc_offer error")

@socketio.on('webrtc_answer')
def on_webrtc_answer(data):
    try:
        sid = request.sid
        payload = {'from': sid, 'sdp': data.get('sdp')}
        socketio.emit('webrtc_answer', payload, broadcast=True)
    except Exception:
        logging.exception("webrtc_answer error")

@socketio.on('webrtc_candidate')
def on_webrtc_candidate(data):
    try:
        sid = request.sid
        payload = {'from': sid, 'candidate': data.get('candidate')}
        socketio.emit('webrtc_candidate', payload, broadcast=True)
    except Exception:
        logging.exception("webrtc_candidate error")

@app.route('/client-log', methods=['POST'])
def client_log():
    try:
        data = request.get_json() or {}
        logging.error("CLIENT LOG: %s", data)
    except Exception:
        logging.exception("Error handling client log")
    return '', 204

# --- Simple assistant (VARIS) endpoint ---
@app.route('/assistant', methods=['POST'])
def assistant():
    try:
        data = request.get_json() or {}
        text = (data.get('text') or '').strip().lower()
        if not text:
            return jsonify({'resp': "I didn't receive a command."})
        # basic rule-based assistant that can reference server state
        if 'players' in text:
            with state_lock:
                players_list = ', '.join([sid[:6] for sid in players.keys()]) or 'none'
            resp = f"Players online: {players_list}"
        elif 'team' in text:
            resp = "Your team is shown on the HUD next to 'Team'."
        elif 'disable pointer' in text or 'disable pointer lock' in text:
            resp = "Pointer lock will be disabled on the client when requested."
        elif 'help' in text:
            resp = "Commands: players, team, disable pointer lock, help."
        else:
            resp = "Sorry, I didn't understand that. Try 'help' for available commands."
        return jsonify({'resp': resp})
    except Exception:
        logging.exception("assistant error")
        return jsonify({'resp': "Assistant error."}), 500

@app.route('/')
def index():
    return render_template_string(HTML_DATA)

# ============================================================
#                      CLIENT HTML + JS
# ============================================================
HTML_DATA = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Secure Voxel FPS - One File</title>

  <script src="https://cdn.jsdelivr.net/npm/three@0.149.0/build/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.149.0/examples/js/loaders/GLTFLoader.js"></script>
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>

  <style>
    :root{
      --neon:#00fff2; --neon-2:#ff4de6; --bg:#030518;
      --panel-bg: rgba(2,6,15,0.6);
    }
    html,body{height:100%;margin:0;background:linear-gradient(180deg,#021 0%, #001322 100%);font-family:Inter,system-ui,monospace;color:#eafffb;overflow:hidden;}
    canvas{display:block;}
    .neon {
      text-shadow: 0 0 6px rgba(0,255,242,0.6), 0 0 20px rgba(0,255,242,0.12);
    }
    #hud{
      position:fixed; left:16px; bottom:16px; padding:10px 14px; background:var(--panel-bg); border-radius:10px; border:1px solid rgba(0,255,242,0.08);
      box-shadow: 0 6px 30px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.02);
      backdrop-filter: blur(4px);
    }
    #hud .title{font-weight:700;color:var(--neon);font-size:13px}
    #crosshair{position:fixed;top:50%;left:50%;width:10px;height:10px;margin:-5px 0 0 -5px;background:transparent;pointer-events:none;}
    #crosshair:before{content:"";display:block;width:14px;height:2px;background:var(--neon);transform:translate(-6px,4px);box-shadow:0 0 8px var(--neon);}
    #crosshair:after{content:"";display:block;width:2px;height:14px;background:var(--neon);transform:translate(4px,-6px);box-shadow:0 0 8px var(--neon);}
    #overlay{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6);z-index:20;color:var(--neon);font-size:18px;cursor:pointer}
    #overlay.hidden{display:none}
    #playerList{position:fixed;right:16px;top:16px;width:240px;padding:12px;background:var(--panel-bg);border-radius:10px;border:1px solid rgba(255,77,230,0.06);box-shadow:0 8px 30px rgba(0,0,0,0.6);color:#cffffa;font-size:13px}
    .playerEntry{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed rgba(255,255,255,0.02)}
    .status.alive{color:#9ff49f}
    .status.dead{color:#ff6b6b}
    .status.fight{color:#ffd166}
    #chat{position:fixed;left:16px;top:16px;width:320px;max-height:40vh;padding:8px;background:var(--panel-bg);border-radius:10px;overflow:auto;font-size:13px}
    #chatInput{position:fixed;left:16px;bottom:80px;width:320px;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.04);background:rgba(0,0,0,0.45);color:#eafffb}
    #voiceBtn{position:fixed;right:16px;bottom:16px;padding:10px 14px;border-radius:8px;background:linear-gradient(90deg,var(--neon),var(--neon-2));color:#001;cursor:pointer;font-weight:700}
    #helpPanel{position:fixed;left:50%;top:18px;transform:translateX(-50%);background:rgba(0,0,0,0.6);padding:8px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);color:#cff;display:none}
    #controls{position:fixed;left:16px;bottom:130px;width:320px;padding:8px;background:rgba(0,0,0,0.55);border-radius:8px;border:1px solid rgba(255,255,255,0.03);color:#dff;font-size:13px}
    #controls h4{margin:0 0 6px 0;color:var(--neon)}
    .controlRow{display:flex;justify-content:space-between;padding:2px 0}
    .keyBadge{background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:4px;border:1px solid rgba(255,255,255,0.02);font-weight:700}
    .neonBadge{padding:4px 8px;border-radius:6px;background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.02);box-shadow:0 4px 20px rgba(0,255,242,0.06);color:var(--neon)}
    /* small mobile-friendly */
    @media (max-width:600px){ #playerList{display:none} #chat{width:70vw} #chatInput{width:70vw} #controls{display:none} }
  </style>
</head>
<body>
  <div id="overlay">CLICK TO DEPLOY INTO THE GRID</div>
  <div id="crosshair" aria-hidden="true"></div>

  <div id="hud" class="neon">
    <div class="title">REVOLVER - HP: <span id="hp">100</span></div>
    <div style="font-size:12px;margin-top:6px">Ammo: <span id="ammoMag">6</span>/<span id="ammoRes">36</span> &nbsp; <span id="teamBadge" class="neonBadge">Team -</span></div>
  </div>

  <div id="playerList"><strong>Players</strong><div id="playersInner"></div></div>

  <div id="chat"></div>
  <input id="chatInput" placeholder="Press Enter to send chat" />

  <div id="helpPanel">
    <div><strong>Help / Voice Commands</strong></div>
    <div style="font-size:13px;margin-top:6px">Say: "VARIS" (alone) then a command: "players" / "team" / "disable pointer lock" / "help"</div>
  </div>

  <div id="controls" aria-hidden="false">
    <h4>Manual Controls</h4>
    <div class="controlRow"><div>Move</div><div class="keyBadge">W A S D</div></div>
    <div class="controlRow"><div>Jump</div><div class="keyBadge">Space</div></div>
    <div class="controlRow"><div>Aim</div><div class="keyBadge">Mouse</div></div>
    <div class="controlRow"><div>Shoot</div><div class="keyBadge">Left Click</div></div>
    <div class="controlRow"><div>Toggle Pointer Lock</div><div class="keyBadge">P</div></div>
    <div class="controlRow"><div>Open Chat</div><div class="keyBadge">/</div></div>
    <div class="controlRow"><div>Help Panel</div><div class="keyBadge">H</div></div>
    <div class="controlRow"><div>Voice (wake + command)</div><div class="keyBadge">Say "VARIS" (alone) or press V to activate</div></div>
    <div class="controlRow"><div>Voice Capture</div><div class="keyBadge">Cmd/Ctrl + V (after VARIS)</div></div>
    <div style="font-size:12px;margin-top:6px;color:#bfe">Tip: Press C to show/hide this panel</div>
  </div>

  <div id="voiceBtn">Start Voice Chat</div>

  <script>
  (function(){
    // logging helper
    function clientLog(obj){
      try { navigator.sendBeacon('/client-log', JSON.stringify(obj)); } catch(e){ try { fetch('/client-log',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)}); }catch(_){} }
    }
    window.addEventListener('error', function(e){ clientLog({type:'err', msg: e.message, stack: e.error && e.error.stack}); });

    const socket = (typeof io === 'function') ? io({transports:['polling'], upgrade:false}) : { on:()=>{}, emit:()=>{} };
    let myId = null;
    let pointerLocked = false;
    let pointerLockEnabled = true; // voice command can toggle
    let micStream = null;
    let mediaRecorder = null;
    let peers = {}; // reserved for future WebRTC usage
    let localAudioTrack = null;
    let voiceActive = false;

    socket.on('connect', ()=>{ myId = socket.id; socket.emit('join'); });
    socket.on('chat', (m)=>{ addChatMessage(m.from, m.text); });
    socket.on('world_init', (d)=>{ handleWorldInit(d); });

    // incoming voice broadcast (dataURL chunks)
    socket.on('voice', (msg)=>{
      try {
        if(!msg || !msg.data) return;
        if(msg.from === myId) return;
        const audio = new Audio(msg.data);
        audio.volume = 1.0;
        audio.play().catch(()=>{ /* ignore */ });
      } catch(e){ clientLog({type:'voice_play_err', err: e && e.stack}); }
    });

    socket.on('webrtc_offer', async (data)=>{
      if(!data || data.from === myId) return;
      try {
        const pc = createPeer(data.from, false);
        await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        socket.emit('webrtc_answer', { sdp: pc.localDescription });
      } catch(e){ clientLog({type:'webrtc_offer', err: e && e.stack}); }
    });
    socket.on('webrtc_answer', async (data)=>{
      if(!data || data.from === myId) return;
      const pc = peers[data.from];
      if(!pc) return;
      try { await pc.setRemoteDescription(new RTCSessionDescription(data.sdp)); } catch(e){ clientLog({type:'webrtc_answer', err: e && e.stack}); }
    });
    socket.on('webrtc_candidate', async (data)=>{
      if(!data || data.from === myId) return;
      const pc = peers[data.from];
      if(!pc) return;
      try { await pc.addIceCandidate(new RTCIceCandidate(data.candidate)); } catch(e){ clientLog({type:'webrtc_candidate', err: e && e.stack}); }
    });

    // simple UI helpers
    const overlay = document.getElementById('overlay');
    const hpSpan = document.getElementById('hp');
    const ammoMagSpan = document.getElementById('ammoMag');
    const ammoResSpan = document.getElementById('ammoRes');
    const playersInner = document.getElementById('playersInner');
    const teamBadge = document.getElementById('teamBadge');
    const chatDiv = document.getElementById('chat');
    const chatInput = document.getElementById('chatInput');
    const voiceBtn = document.getElementById('voiceBtn');
    const helpPanel = document.getElementById('helpPanel');
    const controlsPanel = document.getElementById('controls');

    // TTS helper (VARIS speech)
    function speakText(text){
      try {
        if(!('speechSynthesis' in window)) return;
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(String(text || ''));
        u.lang = 'en-US';
        u.rate = 1.0;
        u.pitch = 1.0;
        u.volume = 1.0;
        const voices = window.speechSynthesis.getVoices();
        if(voices && voices.length){
          u.voice = voices.find(v => /google|en-US|female/i.test(v.name)) || voices[0];
        }
        window.speechSynthesis.speak(u);
      } catch(err){
        clientLog({type:'tts_err', err: err && err.stack});
      }
    }

    function addChatMessage(from, text){
      const el = document.createElement('div');
      el.style.padding='6px 0';
      el.innerHTML = '<strong style="color:#9ff">'+(from||'')+'</strong>: <span>'+escapeHtml(text)+'</span>';
      chatDiv.appendChild(el);
      chatDiv.scrollTop = chatDiv.scrollHeight;
      try {
        const who = (from||'').toString().toLowerCase();
        if(who === 'varis' || who === 'voice'){
          speakText(text);
        }
      } catch(e){ clientLog({type:'tts_trigger_err', err: e && e.stack}); }
    }
    function escapeHtml(s){ return (s||'').replace(/[&<>"']/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]; }); }

    chatInput.addEventListener('keydown', (e)=>{
      if(e.key === 'Enter'){
        e.preventDefault();
        const txt = chatInput.value.trim();
        if(txt){
          const name = socket.id ? socket.id.slice(0,6) : 'me';
          // show locally immediately
          addChatMessage(name, txt);
          socket.emit('chat', { text: txt, from: name });
          chatInput.value='';
        }
      }
    });

    function handleWorldInit(data){
      try {
        const blocks = (data && data.world) || [];
        blocks.forEach(b=>{
          const pos = b.pos; const key = pos.join(',');
          if(!worldBlocks[key]){
            const geo = new THREE.BoxGeometry(1,1,1);
            const mat = new THREE.MeshStandardMaterial({ color: 0x4b5563 });
            const m = new THREE.Mesh(geo, mat);
            m.position.set(pos[0]+0.5, pos[1]+0.5, pos[2]+0.5);
            scene.add(m); worldBlocks[key] = m;
          }
        });
      } catch(e){ clientLog({type:'world_init_handler', err: e && e.stack}); }
    }

    // basic three.js scene + visible revolver
    let scene, camera, renderer, revolver=null, muzzle=null;
    try {
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x041025);
      camera = new THREE.PerspectiveCamera(75, window.innerWidth/window.innerHeight, 0.1, 1000);
      camera.rotation.order='YXZ';
      renderer = new THREE.WebGLRenderer({antialias:true});
      renderer.setSize(window.innerWidth, window.innerHeight);
      document.body.appendChild(renderer.domElement);
      const hemi = new THREE.HemisphereLight(0x88aaff, 0x202020, 0.7); scene.add(hemi);
      const dir = new THREE.DirectionalLight(0xfff2d0, 0.8); dir.position.set(10,20,5); scene.add(dir);
      const ground = new THREE.Mesh(new THREE.PlaneGeometry(80,80), new THREE.MeshStandardMaterial({color:0x1a1f28, roughness:0.95}));
      ground.rotation.x = -Math.PI/2; scene.add(ground);

      // load revolver
      const loader = new THREE.GLTFLoader();
      loader.load('/static/Revolver.glb', (gltf)=>{
        revolver = gltf.scene;
        revolver.traverse(c=>{ if(c.isMesh){ c.castShadow=true; c.receiveShadow=false; }});
        revolver.scale.set(0.6,0.6,0.6);
        revolver.position.set(0.35,-0.35,-0.65);
        revolver.rotation.set(-0.15, Math.PI, 0.05);
        camera.add(revolver);
        muzzle = new THREE.PointLight(0xffddaa, 0, 2);
        muzzle.position.set(0.52,-0.25,-0.95);
        camera.add(muzzle);
      }, undefined, ()=>{ /* ignore */ });
    } catch(e){ clientLog({type:'three_init', err: e && e.stack}); }

    // world and remote players representation
    const worldBlocks = {};
    const remotes = {};

    // inputs and controls
    let keys = {};
    let canShoot = true, fireCooldown = 0, isReloading=false, ammoInMag=6, ammoRes=36;
    let currentWeapon = { name: "REVOLVER", magSize: 6, fireDelay: 0.28, reloadTime: 1.4 };

    function updateAmmoHUD(){ ammoMagSpan.textContent = ammoInMag; ammoResSpan.textContent = ammoRes; document.getElementById('hp').textContent = window.myHp||100; }
    updateAmmoHUD();

    // pointer lock
    overlay.addEventListener('click', ()=>{ overlay.classList.add('hidden'); if(pointerLockEnabled) document.body.requestPointerLock && document.body.requestPointerLock(); });

    document.addEventListener('pointerlockchange', ()=>{ pointerLocked = (document.pointerLockElement === document.body); if(!pointerLocked && !window.myDead) overlay.classList.remove('hidden'); });

    window.addEventListener('keydown', (e)=>{
      // allow typing in chat without moving the player when focused
      if(document.activeElement === chatInput) return;
      keys[e.code] = true;
      if(e.code === 'Space'){ // handle jump as keydown event; client will send single-frame 'jump'
        // set a transient flag that will be sent with next inputs
        keys['JumpPressed'] = true;
      }
      if(e.key === '1'){ /* switch weapon */ }
      if(e.key === '/'){ chatInput.focus(); }
      if(e.code === 'KeyH'){ helpPanel.style.display = helpPanel.style.display === 'block' ? 'none' : 'block'; }
      if(e.key === 'p'){ // toggle pointer lock by key
        pointerLockEnabled = !pointerLockEnabled;
        if(!pointerLockEnabled){ document.exitPointerLock && document.exitPointerLock(); overlay.classList.remove('hidden'); }
      }
      // toggle controls panel
      if(e.key.toLowerCase() === 'c'){
        controlsPanel.style.display = controlsPanel.style.display === 'none' ? 'block' : 'none';
      }

      // Manual activation of VARIS: plain 'v' sets assistant active window (if VARIS voice fails)
      if(!e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === 'v'){
        // if recognition exists, start single-listen mode; otherwise just activate assistant window
        try {
          assistantWakeUntil = Date.now() + WAKE_TIMEOUT;
          addChatMessage('VARIS', 'Assistant manually activated — speak your command or press Cmd/Ctrl+V to capture');
          // optionally start recognition in single mode to capture immediate speech
          if(typeof recognition !== 'undefined' && recognition){
            voiceSessionMode = 'single';
            try { startRecognitionOnce(); } catch(_) {}
          }
        } catch(err){ clientLog({type:'varis_manual_activate_err', err: err && err.stack}); }
      }

      // ctrl/cmd+v voice-capture shortcut (overrides paste to show collected voice input)
      if((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v'){
        if(typeof recognition !== 'undefined' && recognition){
          e.preventDefault();
          voiceBuffer = '';
          voiceSessionMode = 'capture';
          try { startRecognitionOnce(); } catch(_) {}
          // stop after a short capture window
          setTimeout(()=>{ try{ stopRecognitionOnce(); }catch(_){} }, 2500);
        }
      }

      // legacy: simple 'v' also starts single voice command when recognition available (kept for convenience)
      // note: manual activation above already sets assistantWakeUntil
      // simple 'v' to start single voice command (no "Listening" chat message)
      // (this will run too; it's fine because manual activation was handled first)
      if(!e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'v'){
        if(typeof recognition !== 'undefined' && recognition){
          voiceSessionMode = 'single';
          try { startRecognitionOnce(); } catch(_) {}
        }
      }
    });
    window.addEventListener('keyup', (e)=>{ if(document.activeElement === chatInput) return; keys[e.code] = false; });

    // mouse look
    window.addEventListener('mousemove', (e)=>{
      if(!pointerLocked) return;
      const sens = 0.0023;
      camera.rotation.y -= e.movementX * sens;
      camera.rotation.x -= e.movementY * sens;
      camera.rotation.x = Math.max(-1.5, Math.min(1.5, camera.rotation.x));
    });

    // shooting
    window.addEventListener('mousedown', (e)=>{
      if(e.button === 0){
        if(!pointerLocked || window.myDead) return;
        if(!canShoot || isReloading) return;
        if(ammoInMag <= 0){ return; }
        ammoInMag -= 1; updateAmmoHUD();
        socket.emit('shoot');
        playGunSound();
        triggerMuzzle();
        canShoot = false; fireCooldown = currentWeapon.fireDelay;
      }
    });

    function playGunSound(){ const a = new Audio('/static/gunshot.wav'); a.volume=0.9; a.play().catch(()=>{}); }
    function triggerMuzzle(){ if(muzzle){ muzzle.intensity = 4; setTimeout(()=>muzzle.intensity=0,80); } }

    function sendInputs(){
      // pack movement keys and jump pulse
      const keysPack = { w: !!keys['KeyW'], a: !!keys['KeyA'], s: !!keys['KeyS'], d: !!keys['KeyD'] };
      if(keys['JumpPressed']){ keysPack['jump'] = true; keys['JumpPressed'] = false; }
      socket.emit('input', { keys: keysPack, yaw: camera.rotation.y, pitch: camera.rotation.x, jump: !!keysPack['jump'] });
    }

    // handle server state updates
    socket.on('state', (data)=>{
      try {
        const playersData = data.players || {};
        // update local world blocks
        const worldDiff = data.worldDiff || {};
        (worldDiff.changes || []).forEach(c=>{
          const pos = c.pos; const key = pos.join(',');
          if(c.val === 0){ // removed
            if(worldBlocks[key]){ scene.remove(worldBlocks[key]); delete worldBlocks[key]; }
          } else {
            if(!worldBlocks[key]){
              const geo = new THREE.BoxGeometry(1,1,1);
              const mat = new THREE.MeshStandardMaterial({ color: 0x4b5563 });
              const m = new THREE.Mesh(geo, mat);
              m.position.set(pos[0]+0.5, pos[1]+0.5, pos[2]+0.5);
              scene.add(m); worldBlocks[key] = m;
            }
          }
        });

        // update players
        playersInner.innerHTML = '';
        let activeCount = 0;
        for(const id in playersData){
          activeCount++;
          const p = playersData[id];
          const entry = document.createElement('div');
          entry.className = 'playerEntry';
          const name = document.createElement('div'); name.textContent = id.slice(0,6) + (id===myId ? ' (you)' : '');
          const stat = document.createElement('div');
          stat.className = 'status';
          if(p.dead) stat.classList.add('dead'), stat.textContent='dead';
          else if(p.in_fight) stat.classList.add('fight'), stat.textContent='in fight';
          else stat.classList.add('alive'), stat.textContent='alive';
          entry.appendChild(name); entry.appendChild(stat);
          const teamDiv = document.createElement('div'); teamDiv.textContent = p.team || '?'; teamDiv.style.marginLeft='8px'; entry.appendChild(teamDiv);
          playersInner.appendChild(entry);

          // remote 3D avatar
          if(id !== myId){
            if(!remotes[id]){
              const geo = new THREE.BoxGeometry(0.5,1,0.5);
              const mat = new THREE.MeshStandardMaterial({ color: p.team === 'A' ? 0xff7f7f : 0x7fbfff });
              const m = new THREE.Mesh(geo, mat);
              scene.add(m); remotes[id] = { mesh: m };
            }
            const m = remotes[id].mesh;
            m.position.set(p.pos[0], p.pos[1]-0.5, p.pos[2]);
            m.material.color.set(p.dead ? 0x222222 : (p.team === 'A' ? 0xff7f7f : 0x7fbfff));
          } else {
            // update local HUD info
            hpSpan.textContent = p.hp;
            teamBadge.textContent = 'Team ' + (p.team || '?');
          }
        }
      } catch(e){ clientLog({type:'state-handler', err: e && e.stack}); }
    });

    socket.on('hit', (d)=>{ showHitmarker(); });

    socket.on('block_break', (d)=>{ const a = new Audio('/static/blockbreak.wav'); a.play().catch(()=>{}); });

    function showHitmarker(){ const el = document.getElementById('crosshair'); el.style.transform='scale(1.6)'; setTimeout(()=>el.style.transform='scale(1)', 140); const s = new Audio('/static/hitmarker.wav'); s.volume=0.6; s.play().catch(()=>{}); }

    // animation loop
    let lastTime = performance.now();
    function animate(now){
      requestAnimationFrame(animate);
      const dt = (now - lastTime)/1000; lastTime = now;
      try {
        // send inputs at each frame if pointer locked or pointerLock disabled
        if(pointerLocked || !pointerLockEnabled) sendInputs();

        if(!canShoot){
          fireCooldown -= dt;
          if(fireCooldown <= 0) canShoot = true;
        }
        // render
        if(renderer && scene && camera) renderer.render(scene, camera);
      } catch(e){ clientLog({type:'animate', err: e && e.stack}); }
    }
    requestAnimationFrame(animate);

    // simple WebRTC helpers (kept for optional use)
    function createPeer(peerId, isCaller){
      if(peers[peerId]) return peers[peerId];
      const pc = new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
      pc.onicecandidate = (evt)=>{
        if(evt.candidate) socket.emit('webrtc_candidate', { candidate: evt.candidate });
      };
      pc.ontrack = (evt)=>{
        let audioEl = document.getElementById('audio-'+peerId);
        if(!audioEl){
          audioEl = document.createElement('audio');
          audioEl.id = 'audio-'+peerId;
          audioEl.autoplay = true; audioEl.controls = false;
          audioEl.style.display='none';
          document.body.appendChild(audioEl);
        }
        audioEl.srcObject = evt.streams[0];
      };
      // attach local track if available
      if(localAudioTrack){
        try { pc.addTrack(localAudioTrack, micStream); } catch(_) {}
      }
      peers[peerId] = pc;
      return pc;
    }

    // Voice chat using MediaRecorder + socket broadcast (simple, cross-browser friendly)
    async function startVoiceChat(){
      if(voiceActive) return stopVoiceChat();
      try {
        micStream = await navigator.mediaDevices.getUserMedia({audio:true});
        localAudioTrack = micStream.getAudioTracks()[0];
        voiceActive = true;
        voiceBtn.textContent = 'Stop Voice Chat';

        // create MediaRecorder to capture small chunks and emit as dataURL
        const options = { mimeType: 'audio/webm;codecs=opus' };
        try { mediaRecorder = new MediaRecorder(micStream, options); } catch(e){ mediaRecorder = new MediaRecorder(micStream); }
        mediaRecorder.ondataavailable = (ev)=>{
          if(!ev.data || ev.data.size === 0) return;
          const reader = new FileReader();
          reader.onloadend = ()=> {
            const dataUrl = reader.result;
            // send as JSON-friendly dataURL (simple, works without binary socket support)
            try { socket.emit('voice', { data: dataUrl }); } catch(err){ clientLog({type:'voice_emit_err', err: err && err.stack}); }
          };
          reader.readAsDataURL(ev.data);
        };
        mediaRecorder.start(250); // emit every 250ms
        // notify
        addChatMessage('VOICE', 'Voice chat started (broadcast to other players).');
      } catch(e){ clientLog({type:'startVoice', err: e && e.stack}); alert('Microphone access denied or not available.'); }
    }
    function stopVoiceChat(){
      try {
        if(mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
      } catch(_) {}
      if(micStream){ micStream.getTracks().forEach(t=>t.stop()); micStream=null; localAudioTrack=null; }
      mediaRecorder = null;
      voiceActive=false; voiceBtn.textContent = 'Start Voice Chat';
      addChatMessage('VOICE', 'Voice chat stopped.');
    }
    voiceBtn.addEventListener('click', ()=>{ if(voiceActive) stopVoiceChat(); else startVoiceChat(); });

    // basic speech recognition voice commands with STRICT wake word "VARIS" (must be said alone)
    let recognition = null;
    let recognitionRunning = false;
    let recognitionRestartTimer = null;
    let voiceBuffer = '';
    let voiceSessionMode = null; // 'single' or 'capture' or null
    let assistantWakeUntil = 0; // ms timestamp until assistant is active
    const WAKE_TIMEOUT = 6000; // 6s window after saying "VARIS"

    // helper: start/stop recognition safely (debounced)
    function startRecognitionOnce(){
      if(!recognition || recognitionRunning) return;
      try {
        recognition.start();
      } catch(err){
        clientLog({type:'speech_start_err', err: err && err.stack});
      }
    }
    function stopRecognitionOnce(){
      if(!recognition || !recognitionRunning) return;
      try {
        recognition.stop();
      } catch(err){
        clientLog({type:'speech_stop_err', err: err && err.stack});
      }
    }

    // helper to send text to server-side "VARIS" AI and display reply
    async function sendToAssistant(text){
      try {
        const res = await fetch('/assistant', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ text: text })
        });
        const j = await res.json();
        addChatMessage('VARIS', j.resp || '');
      } catch(err){
        clientLog({type:'assistant_fetch_err', err: err && err.stack});
        addChatMessage('VARIS', 'Assistant error.');
      }
    }

    try {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if(SpeechRecognition){
        recognition = new SpeechRecognition();
        recognition.lang = 'en-US';
        recognition.continuous = true;
        recognition.interimResults = true;

        // Keep recognition running; restart on end to achieve always-on behavior
        const RESTART_ON_END = true;
        const RESTART_DELAY_MS = 400; // small debounce to avoid tight loops

        recognition.onstart = ()=>{
          recognitionRunning = true;
          if(recognitionRestartTimer){ clearTimeout(recognitionRestartTimer); recognitionRestartTimer = null; }
        };

        recognition.onresult = (e)=>{
          try {
            let combined = '';
            for(let i=0;i<e.results.length;i++){
              combined += e.results[i][0].transcript;
            }
            combined = combined.trim();
            const lower = (combined||'').toLowerCase();

            // STRICT wake-word detection: only the single word "varis" (nothing else) activates assistant
            const lowerTrim = lower.replace(/^[\s\W]+|[\s\W]+$/g, '');
            if(lowerTrim === 'varis'){
              assistantWakeUntil = Date.now() + WAKE_TIMEOUT;
              addChatMessage('VARIS','Assistant activated — say your command or use the keyboard shortcuts');
              // clear combined because wake word was standalone
              combined = '';
            }

            const assistantActive = Date.now() < assistantWakeUntil;

            if(voiceSessionMode === 'capture'){
              // only accumulate captured speech if assistant was activated first
              if(assistantActive){
                voiceBuffer = (voiceBuffer + ' ' + combined).trim();
              } else {
                // ignore capture if not activated; silently drop partials
              }
            } else {
              // single immediate command: only act if assistant was activated
              if(assistantActive){
                if(combined.length > 0){
                  clientLog({type:'voice_command', text: combined});
                  // send to VARIS (AI) and also perform client actions
                  sendToAssistant(combined);
                  handleVoiceCommand(combined);
                  // consume the wake window so user must say VARIS again for next command
                  assistantWakeUntil = 0;
                  // keep recognition running for future wake words
                }
              } else {
                // user attempted a single command without wake word
                // give a short hint in chat and do not run commands
                if(combined.length > 0){
                  addChatMessage('VOICE','Say "VARIS" by itself first to activate the assistant');
                }
              }
            }
          } catch(err){ clientLog({type:'speech_onresult_err', err: err && err.stack}); }
        };

        recognition.onerror = (e)=> clientLog({type:'speech_err', err:e});
        recognition.onend = ()=>{
          recognitionRunning = false;
          try {
            const assistantActive = Date.now() < assistantWakeUntil;
            if(voiceSessionMode === 'capture'){
              // only send captured buffer to assistant if assistant was active during capture
              if(assistantActive && voiceBuffer && voiceBuffer.length > 0){
                sendToAssistant(voiceBuffer);
                handleVoiceCommand(voiceBuffer);
                // consume wake window after capture
                assistantWakeUntil = 0;
              }
              voiceBuffer = '';
              voiceSessionMode = null;
            } else {
              voiceSessionMode = null;
            }
          } catch(err){ clientLog({type:'speech_onend_err', err: err && err.stack}); }

          // Restart recognition automatically to achieve always-on listening behavior,
          // but debounce/retry after a short delay to avoid tight start/stop loops.
          if(RESTART_ON_END){
            if(recognitionRestartTimer) clearTimeout(recognitionRestartTimer);
            recognitionRestartTimer = setTimeout(()=>{
              try{ startRecognitionOnce(); }catch(err){ clientLog({type:'speech_restart_err', err: err && err.stack}); }
            }, RESTART_DELAY_MS);
          }
        };

        // start listening immediately (browser will prompt for permission)
        try { startRecognitionOnce(); addChatMessage('VOICE','VARIS is listening (always-on)'); } catch(err){ clientLog({type:'speech_start_err', err: err && err.stack}); }
      }
    } catch(e){ clientLog({type:'speech_init', err: e && e.stack}); }

    function handleVoiceCommand(t){
      const text = (t||'').toLowerCase().trim();
      if(text.length === 0) return;
      if(text.includes('players')){
        showPlayersList();
        addChatMessage('VOICE','Showing players list');
      } else if(text.includes('team')){
        addChatMessage('VOICE','You are on ' + (teamBadge.textContent || 'unknown'));
      } else if(text.includes('disable pointer') || text.includes('disable pointer lock')){
        pointerLockEnabled = false; document.exitPointerLock && document.exitPointerLock(); overlay.classList.remove('hidden'); addChatMessage('VOICE','Pointer lock disabled');
      } else if(text.includes('help')){
        helpPanel.style.display = 'block'; addChatMessage('VOICE','Help opened');
      } else {
        addChatMessage('VOICE','Unknown command: ' + text);
      }
    }

    // NOTE: 'VARIS' must be spoken alone to enable assistant.
    // - Say "VARIS" then press V for a single command.
    // - Say "VARIS" then press Cmd/Ctrl+V to capture a short speech segment which will be sent to VARIS.
    // - If VARIS voice activation fails, press plain V to manually activate VARIS (shows message and can auto-start recognition).

    function showPlayersList(){
      // briefly highlight player list
      const el = document.getElementById('playerList');
      el.style.boxShadow = '0 0 40px rgba(0,255,242,0.25)';
      setTimeout(()=>{ el.style.boxShadow = ''; }, 2000);
    }

    // helper for pointer lock toggle by voice button (exposed)
    window.togglePointerLock = function(enabled){
      pointerLockEnabled = !!enabled;
      if(!pointerLockEnabled) document.exitPointerLock && document.exitPointerLock();
    };

    // cleanup on unload
    window.addEventListener('beforeunload', ()=>{ try{ socket.emit('disconnect'); }catch(_){} });

  })();
  </script>
</body>
</html>
"""
# ...existing code...
if __name__ == '__main__':
    logging.info("Starting server")
    try:
        socketio.run(app, host='0.0.0.0', port=5000, debug=False)
    except Exception:
        logging.exception("Server crashed on run")
