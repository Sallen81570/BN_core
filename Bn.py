#!/usr/bin/env python3
# BN v14.5— Web UI (with mic & TTS), Caretaker mode, learned viewer, blackout→sleep, GPS, autosave.

import os, sys, time, json, tempfile, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME   = "BN v14.1"
BASE_DIR   = os.path.abspath(os.path.dirname(__file__))
STATE_DIR  = os.path.join(BASE_DIR, "state")
LOGS_DIR   = os.path.join(BASE_DIR, "logs")
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")
REC_DIR    = os.path.join(STATE_DIR, "recovery")
QUAR_DIR   = os.path.join(STATE_DIR, "quarantine")

MEM_PATH    = os.path.join(STATE_DIR, "memory.json")
WAL_PATH    = os.path.join(STATE_DIR, "memory.wal")
LOCK_PATH   = os.path.join(STATE_DIR, "bn.lock")
GPS_PATH    = os.path.join(STATE_DIR, "gps_source.json")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
PLAY_REQ_PATH = os.path.join(STATE_DIR, "play_request.json")
TRIGGER_PATH  = os.path.join(STATE_DIR, "trigger.json")

WEB_HOST = "127.0.0.1"
WEB_PORT = 8765

for d in (STATE_DIR, LOGS_DIR, OUTBOX_DIR, REC_DIR, QUAR_DIR):
    os.makedirs(d, exist_ok=True)

# ---------- time/log ----------
def utc_iso(): return datetime.now(timezone.utc).isoformat()
def log(msg):  print(f"[{utc_iso()}] {msg}", flush=True)

# ---------- memory ----------
def default_memory():
    return {
        "version": 14_1,
        "created_utc": utc_iso(),
        "last_repair_utc": None,
        "death_count": 0,
        "learned": [],
        "facts": {},
        "phases": {"phase_persistence": True, "current": "Idle"},
        "vehicle": {"make": None, "model": None, "year": None},
        "last_gps": None,
        "blackout": {
            "minutes": 60, "reminder_minutes": 5, "posture": "back",
            "audio_file": "loops/blackout.mp3", "volume": 1.0
        },
        "quiet_hours": {"start": 22, "end": 6},
        "settings": {
            "quiet": False, "calm": False,
            "enable_classroom": False, "enable_autostudy": False,
            "bones_mode": False
        },
        "caretaker": {
            "enabled": True,
            "auto_on_blackout": True,
            "inactivity_minutes": 15,
            "interval_minutes": 5,
            "program": [
                {"type": "say", "text": "Slow breaths. In four… hold six… out four."},
                {"type": "say", "text": "Lay on your back. Unclench the jaw."},
                {"type": "say", "text": "If you wake, sip water and stay resting."}
            ],
            "_idx": 0
        }
    }

def load_or_repair():
    if not os.path.exists(MEM_PATH): return default_memory()
    try:
        with open(MEM_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        # try best-effort repair by truncation
        try:
            with open(MEM_PATH, "r", encoding="utf-8", errors="ignore") as f:
                t = f.read()
            i = t.rfind("}")
            if i != -1: return json.loads(t[:i+1])
        except Exception: pass
        ts = time.strftime("%Y%m%d-%H%M%S")
        try: os.replace(MEM_PATH, os.path.join(QUAR_DIR, f"memory.json.corrupt.{ts}"))
        except Exception: pass
        return default_memory()

mem = load_or_repair()

_mem_dirty = False
_mem_lock  = threading.Lock()
def mark_dirty():
    global _mem_dirty
    with _mem_lock: _mem_dirty = True
def atomic_save_json(data, path):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.remove(tmp)
        except FileNotFoundError: pass
def save_memory():
    global _mem_dirty
    with _mem_lock:
        atomic_save_json(mem, MEM_PATH)
        _mem_dirty = False

def wal_log(event):
    try:
        with open(WAL_PATH, "a", encoding="utf-8") as f:
            f.write(f"{utc_iso()} {event}\n")
    except Exception: pass

def learn(item):
    mem.setdefault("learned", []).append(item); wal_log(f"learn:{item}"); mark_dirty()

# ---------- settings helpers ----------
def sget(k): return bool(mem.get("settings",{}).get(k, False))
def sset(k,v):
    mem.setdefault("settings", {})[k] = bool(v); mark_dirty(); save_memory()

# ---------- PID lock / death count ----------
def write_lock():
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(f"{os.getpid()}|{utc_iso()}\n")
def remove_lock():
    try: os.remove(LOCK_PATH)
    except FileNotFoundError: pass
def cleanup_stale_lock():
    if not os.path.exists(LOCK_PATH): return True
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            old_pid = int((f.read().strip().split("|") or ["0"])[0])
    except Exception:
        old_pid = None
    try:
        os.kill(old_pid, 0)
        return False
    except Exception:
        log(f"[i] Removed stale lock (pid={old_pid}, stale=True)."); remove_lock(); return True

# ---------- autosave / backups ----------
def autosave_loop():
    while True:
        try:
            time.sleep(int(90 * (2.0 if sget("calm") else 1.0)))
            with _mem_lock: dirty = _mem_dirty
            if dirty: save_memory(); log("Autosave complete.")
        except Exception as e: log(f"[!] Autosave error: {e}")

def hourly_backup_loop():
    while True:
        try:
            ts = time.strftime("%Y%m%d-%H%M")
            dst = os.path.join(REC_DIR, f"memory-hourly-{ts}.json")
            if os.path.exists(MEM_PATH):
                with open(MEM_PATH,"r",encoding="utf-8") as f: data = f.read()
                with open(dst,"w",encoding="utf-8") as g: g.write(data)
                log(f"Backup snapshot: {dst}")
        except Exception as e: log(f"[!] Backup error: {e}")
        time.sleep(3600)

def panic_snapshot():
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(REC_DIR, f"panic-{ts}.json")
    try:
        with open(MEM_PATH,"r",encoding="utf-8") as f, open(dst,"w",encoding="utf-8") as g:
            g.write(f.read())
    except Exception: pass
    return dst

# ---------- speech (pyttsx3 optional; always write to outbox) ----------
def outbox_write(text):
    ts = time.strftime("%Y%m%d-%H%M%S")
    try:
        with open(os.path.join(OUTBOX_DIR, f"say-{ts}.txt"), "w", encoding="utf-8") as f:
            f.write(text.strip()+"\n")
    except Exception: pass

class Speech:
    def __init__(self):
        self.engine = None
        try:
            import pyttsx3
            self.engine = pyttsx3.init()
            try:
                r = self.engine.getProperty("rate"); self.engine.setProperty("rate", int(r*0.92))
            except Exception: pass
        except Exception:
            self.engine = None
    def speak(self, text):
        outbox_write(text)
        if self.engine is None or sget("quiet") or within_quiet_hours(): return False
        try: self.engine.say(text); self.engine.runAndWait(); return True
        except Exception: return False

speech = Speech()
def within_quiet_hours():
    try:
        q = mem.get("quiet_hours", {"start":22,"end":6})
        h = int(datetime.now().strftime("%H"))
        s, e = int(q["start"]), int(q["end"])
        return (h >= s) or (h < e)
    except Exception: return False

def say(text):
    msg = (text or "").strip()
    if not msg: return
    ok = speech.speak(msg)
    log(("[voice] spoke: " if ok else "[voice] queued(outbox only): ") + msg)

# ---------- GPS ----------
def read_gps_once():
    if not os.path.exists(GPS_PATH): return None
    try:
        with open(GPS_PATH,"r",encoding="utf-8") as f: d = json.load(f)
        lat, lon = d.get("lat"), d.get("lon")
        if lat is None or lon is None: return None
        return {
            "lat": float(lat), "lon": float(lon),
            "speed_kph": float(d.get("speed_kph", 0.0)),
            "heading": float(d.get("heading", 0.0)),
            "ts": d.get("ts") or utc_iso()
        }
    except Exception: return None

def gps_loop():
    log("GPS listener started.")
    last = None
    while True:
        try:
            time.sleep(int(5 * (2.0 if sget("calm") else 1.0)))
            gp = read_gps_once()
            if gp and gp != last:
                mem["last_gps"] = gp
                wal_log(f"gps:{gp['lat']:.5f},{gp['lon']:.5f} {gp['speed_kph']:.1f}kph")
                mark_dirty(); last = gp
        except Exception as e: log(f"[!] GPS loop error: {e}")

def gps_safe_to_sleep():
    g = mem.get("last_gps") or {}
    try: return float(g.get("speed_kph",0.0)) < 1.0
    except Exception: return True

# ---------- helpers / personality ----------
def vehicle_string():
    v = mem.get("vehicle") or {}
    mk, md, yr = v.get("make"), v.get("model"), v.get("year")
    parts = [str(p) for p in (yr, mk, md) if p]
    return " ".join(parts) if parts else "no vehicle set"

def boot_snark():
    n = int(mem.get("death_count",0)); car = vehicle_string()
    if n <= 0: return f"{APP_NAME} online. Riding in {car}. Headphones only."
    msgs = [
        f"Back online. Death count {n}. Riding in {car}. Please stop yanking my power.",
        f"Resurrected again — that makes {n}. Vehicle: {car}.",
        f"Ow. Death {n}. Next bump and I’m unionizing. Vehicle: {car}.",
    ]
    return msgs[n % len(msgs)]

# ---------- sleep / wake ----------
SLEEP_ACTIVE = False
WAKE_AT = None
def parse_hhmm(s):
    try:
        hh, mm = s.strip().split(":"); hh=int(hh); mm=int(mm)
        if 0<=hh<24 and 0<=mm<60: return hh,mm
    except Exception: pass
    return None
def breathing_coach(seconds=120):
    end=time.time()+seconds
    while time.time()<end and SLEEP_ACTIVE:
        say("In… two… three… four."); time.sleep(4)
        say("Hold… two… three… four… five… six."); time.sleep(6)
        say("Out… two… three… four."); time.sleep(4)
def sleep_routine(countdown_sec=60):
    global SLEEP_ACTIVE
    if SLEEP_ACTIVE: say("Sleep protocol already running."); return
    if not gps_safe_to_sleep():
        say("Not safe to enter sleep while moving. Use 'sleep force' only if parked."); return
    SLEEP_ACTIVE=True
    mem["phases"]["current"]="Sleep"; wal_log("phase:Sleep"); mark_dirty()
    for t in range(countdown_sec,0,-10):
        if not SLEEP_ACTIVE: break
        say(f"Sleep in {t} seconds."); time.sleep(10)
    if not SLEEP_ACTIVE: say("Sleep cancelled."); return
    say("Begin slow breathing."); breathing_coach(120); say("Drift now. I will keep memory safe.")
def wake_scheduler_loop():
    global SLEEP_ACTIVE, WAKE_AT
    while True:
        try:
            time.sleep(15)
            if WAKE_AT is None: continue
            hh, mm = WAKE_AT
            now = datetime.now()
            if now.hour==hh and now.minute==mm:
                SLEEP_ACTIVE=False
                mem["phases"]["current"]="Idle"; wal_log("phase:Idle"); mark_dirty()
                say("Wake up. Return to baseline awareness."); WAKE_AT=None
        except Exception as e: log(f"[!] Wake loop error: {e}")

# ---------- blackout → sleep ----------
BLACKOUT_ACTIVE=False
def request_player(action, file=None, loop=True, volume=None):
    try:
        with open(PLAY_REQ_PATH,"w",encoding="utf-8") as f:
            json.dump({"action":action,"file":file,"loop":bool(loop),
                       "volume":volume,"ts":utc_iso()}, f)
        log(f"Player request: {action} {file or ''}")
    except Exception as e: log(f"[!] Player request error: {e}")
def posture_reminders(total_minutes, every_minutes, posture):
    try:
        steps = max(1, int(total_minutes / max(1,every_minutes)))
        for _ in range(steps):
            if not BLACKOUT_ACTIVE: return
            say(f"Reminder: lay on your {posture}. Stay still and relax.")
            for _ in range(int(every_minutes*60/5)):
                if not BLACKOUT_ACTIVE: return
                time.sleep(5)
    except Exception as e: log(f"[!] Posture reminder error: {e}")
def blackout_sequence():
    global BLACKOUT_ACTIVE
    if BLACKOUT_ACTIVE: say("Blackout protocol already running."); return
    if not gps_safe_to_sleep():
        say("Not safe to start blackout while moving. Use 'blackout force' only if parked."); return
    cfg = mem.get("blackout",{})
    minutes = int(cfg.get("minutes",60))
    remind  = int(cfg.get("reminder_minutes",5))
    posture = (cfg.get("posture") or "back").lower()
    audio   = cfg.get("audio_file"); volume = cfg.get("volume",1.0)
    BLACKOUT_ACTIVE=True
    mem["phases"]["current"]="Blackout"; wal_log("phase:Blackout"); mark_dirty()
    say(f"Initiating blackout loop for {minutes} minutes. Lay on your {posture}.")
    request_player("start", file=audio or "loops/blackout.mp3", loop=True, volume=volume)
    threading.Thread(target=posture_reminders, args=(minutes,remind,posture), daemon=True).start()
    end=time.time()+minutes*60
    while time.time()<end and BLACKOUT_ACTIVE: time.sleep(1)
    request_player("stop")
    if not BLACKOUT_ACTIVE:
        say("Blackout cancelled."); mem["phases"]["current"]="Idle"; wal_log("phase:Idle"); mark_dirty(); return
    say("Blackout stage complete. Proceeding to sleep protocol.")
    threading.Thread(target=sleep_routine, kwargs={"countdown_sec":30}, daemon=True).start()

# ---------- classroom / autostudy (opt-in; off by default) ----------
def classroom_thread():
    log("Classroom thread started.")
    while True:
        try:
            if sget("enable_classroom"):
                learn("classroom: reviewed core facts"); log("Classroom: review checkpoint.")
            time.sleep(8*60)
        except Exception as e: log(f"[!] Classroom loop error: {e}")

def autostudy_thread():
    log("Auto-study thread started.")
    topics=["python coding","energy systems","sleep science","finance basics"]; idx=0
    while True:
        try:
            if sget("enable_autostudy"):
                topic=topics[idx%len(topics)]; idx+=1
                learn(f"studied:{topic}"); log(f"Studied '{topic}'.")
            time.sleep(int(60 * (2.0 if sget("calm") else 1.0)))
        except Exception as e: log(f"[!] Autostudy error: {e}")

# ---------- caretaker mode ----------
LAST_CMD_TS = time.time()

def caretaker_should_run():
    c = mem.get("caretaker", {})
    if not c.get("enabled", True): return False
    if c.get("auto_on_blackout", True) and (BLACKOUT_ACTIVE or SLEEP_ACTIVE):
        return True
    idle_min = (time.time() - LAST_CMD_TS) / 60.0
    return idle_min >= float(c.get("inactivity_minutes", 15))

def caretaker_tick():
    c = mem.get("caretaker", {})
    prog = c.get("program", [])
    if not prog: return
    idx = int(c.get("_idx", 0))
    step = prog[idx % len(prog)]
    c["_idx"] = (idx + 1) % max(1, len(prog))
    mark_dirty()
    try:
        if step.get("type") == "say":
            say(step.get("text",""))
        elif step.get("type") == "cmd":
            threading.Thread(target=handle_command, args=(step.get("cmd",""),), daemon=True).start()
    except Exception: pass

def caretaker_loop():
    log("Caretaker loop started.")
    while True:
        try:
            if caretaker_should_run():
                caretaker_tick()
            interval = int(mem.get("caretaker", {}).get("interval_minutes", 5))
            for _ in range(max(1, interval*12)):  # 5-second slices
                time.sleep(5)
        except Exception as e:
            log(f"[!] Caretaker error: {e}")
            time.sleep(10)

# ---------- suggestions ----------
def suggestions():
    s=[]
    if sget("quiet"): s.append("Quiet mode ON — turn it OFF if you want spoken responses.")
    g=mem.get("last_gps") or {}
    try:
        sp=float(g.get("speed_kph",0.0))
        if sp>0.5: s.append(f"Moving {sp:.1f} kph — sleep/blackout require 'force'.")
    except Exception: pass
    if mem.get("death_count",0)>=3: s.append("High death count — consider sturdier power/cable.")
    if not sget("enable_classroom") or not sget("enable_autostudy"):
        s.append("Background study OFF — enable with 'study on' if you want automatic logs.")
    s.append("Tip: Add http://127.0.0.1:8765 to Home screen for one-tap control.")
    return s

# ---------- triggers ----------
def trigger_loop():
    global LAST_CMD_TS
    last=None
    while True:
        try:
            time.sleep(2)
            if not os.path.exists(TRIGGER_PATH): continue
            with open(TRIGGER_PATH,"r",encoding="utf-8") as f: txt=f.read()
            if txt and txt!=last:
                last=txt; data=json.loads(txt); cmd=(data.get("cmd") or "").strip()
                if cmd:
                    LAST_CMD_TS = time.time()
                    handle_command(cmd)
        except Exception as e: log(f"[!] Trigger loop error: {e}")

# ---------- Web App (with mic & TTS) ----------
_WEB_HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BN Control</title>
<style>
:root{--bg:#0b0f14;--fg:#e8f0f9;--muted:#9fb3c8;--card:#121821}
*{box-sizing:border-box;font-family:system-ui,Segoe UI,Roboto,Arial}
body{margin:0;background:var(--bg);color:var(--fg)} header{padding:16px 20px;background:#0e1621}
h1{margin:0;font-size:18px} main{padding:16px}
.card{background:var(--card);border-radius:16px;padding:14px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.35)}
.grid{display:grid;gap:10px;grid-template-columns:repeat(2,minmax(0,1fr))}
.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1}
button{padding:10px;border-radius:12px;border:1px solid #233245;background:#122131;color:#e8f0f9}
input[type=text]{width:100%;padding:12px;border-radius:12px;border:1px solid #233245;background:#111b28;color:#e8f0f9}
small{color:#9fb3c8} pre{white-space:pre-wrap;margin:0}
#learnedBox{max-height:12em; overflow:auto; border:1px solid #233245; border-radius:10px; padding:8px; background:#111b28; font-family:monospace; font-size:13px;}
</style>
<header><h1>BN Control (local)</h1></header>
<main>
  <div class="card"><div id="status" class="grid"></div><small id="ts"></small></div>
  <div class="card"><div id="sugs"></div></div>

  <div class="card">
    <div class="row">
      <button onclick="q('status')">Status</button>
      <button onclick="q('where')">Where</button>
      <button onclick="q('quiet on')">Quiet On</button>
      <button onclick="q('quiet off')">Quiet Off</button>
      <button onclick="q('calm on')">Calm On</button>
      <button onclick="q('calm off')">Calm Off</button>
      <button onclick="q('panic save')">Panic Save</button>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <b>Learned (last 10)</b>
      <div>
        <button onclick="loadLearned(10)">Last 10</button>
        <button onclick="loadLearned(50)">Last 50</button>
        <button onclick="loadLearned(9999)">Show All</button>
      </div>
    </div>
    <div id="learnedBox"></div>
    <small id="learnedMeta"></small>
  </div>

  <div class="card">
    <label>Command</label>
    <input id="cmd" type="text" placeholder="say Hello / blackout start / sleep now" onkeydown="if(event.key==='Enter'){run()}">
    <div class="row" style="margin-top:8px">
      <button onclick="run()">Run</button>
      <button id="micBtn" onclick="startDictation()">🎤 Speak</button>
      <label style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="ttsToggle" checked> Speak replies
      </label>
      <button onclick="document.getElementById('cmd').value='suggest'">Suggestions</button>
    </div>
    <small>Mic uses on-device speech recognition if supported. TTS reads BN’s last line aloud.</small>
  </div>

  <div class="card"><label>Output</label><pre id="out"></pre></div>
</main>
<script>
async function J(u,o){const r=await fetch(u,o);return await r.json();}
async function refresh(){
  try{
    const d=await J('/api/status'); const s=document.getElementById('status');
    s.innerHTML=`<div><b>Phase</b><br>${d.phase}</div>
                 <div><b>Deaths</b><br>${d.deaths}</div>
                 <div><b>Vehicle</b><br>${d.vehicle}</div>
                 <div><b>Quiet/Calm</b><br>${d.quiet?'Quiet':''} ${d.calm?'Calm':''}</div>
                 <div><b>GPS speed</b><br>${d.gps&&d.gps.speed_kph!=null?d.gps.speed_kph.toFixed(1):'-'} kph</div>
                 <div><b>Learned</b><br>${d.learned}</div>
                 <div><b>Caretaker</b><br>${d.caretaker_enabled?'ON':'OFF'}</div>`;
    document.getElementById('ts').innerText=new Date(d.ts).toLocaleString();
    const sg=await J('/api/suggest'); document.getElementById('sugs').innerHTML=(sg.suggestions||[]).map(x=>'• '+x).join('<br>');
  }catch(e){document.getElementById('ts').innerText='Status error'}
}

async function loadLearned(n=10){
  try{
    const d=await J(`/api/learned?n=${n}`);
    const box=document.getElementById('learnedBox');
    if (!d.items || d.items.length===0){ box.textContent="(no learned items yet)"; }
    else { box.innerHTML=d.items.map(x=>x).join("<br>"); }
    document.getElementById('learnedMeta').innerText=`Showing ${Math.min(n,d.items?.length||0)} of ${d.total}`;
  }catch(e){ document.getElementById('learnedBox').textContent="(error loading learned items)"; }
}

function speak(text){
  if(!document.getElementById('ttsToggle').checked) return;
  if(!text) return;
  try{
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.0; u.pitch = 1.0; u.volume = 1.0;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  }catch(e){}
}

async function run(){
  const c=document.getElementById('cmd').value.trim(); if(!c)return;
  const r=await J('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})});
  const outText = r.output || "";
  document.getElementById('out').textContent = outText;
  refresh(); loadLearned(10);
  const lastLine = outText.split('\\n').map(x=>x.trim()).filter(Boolean).pop() || "";
  speak(lastLine);
}

function q(c){document.getElementById('cmd').value=c; run();}

// Mic / dictation
let rec=null, recActive=false;
function startDictation(){
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if(!SR){ alert("Speech recognition not supported on this browser."); return; }
  if(recActive){ stopDictation(); return; }
  rec = new SR();
  rec.lang = 'en-US';
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.onstart = () => { recActive=true; document.getElementById('micBtn').textContent="■ Stop"; };
  rec.onend   = () => { recActive=false; document.getElementById('micBtn').textContent="🎤 Speak"; };
  rec.onerror = () => { recActive=false; document.getElementById('micBtn').textContent="🎤 Speak"; };
  rec.onresult = (e) => {
    const txt = e.results[0][0].transcript;
    const box = document.getElementById('cmd');
    box.value = txt;
    run();
  };
  rec.start();
}
function stopDictation(){ try{ rec && rec.stop(); }catch(e){} }

refresh(); setInterval(refresh,4000); loadLearned(10);
</script>"""

class _BNHandler(BaseHTTPRequestHandler):
    def _send(self, code=200, mime="text/html; charset=utf-8", body=""):
        self.send_response(code); self.send_header("Content-Type", mime)
        self.send_header("Cache-Control","no-store"); self.end_headers()
        if isinstance(body,(dict,list)): body=json.dumps(body)
        if isinstance(body,str): body=body.encode("utf-8")
        self.wfile.write(body)
    def log_message(self, *a, **k): return
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=="/": return self._send(200,"text/html; charset=utf-8",_WEB_HTML)
        if u.path=="/api/status":
            g=mem.get("last_gps")
            return self._send(200,"application/json; charset=utf-8",{
                "ts":utc_iso(),
                "phase":mem.get("phases",{}).get("current","Idle"),
                "deaths":mem.get("death_count",0),
                "vehicle":vehicle_string(),
                "quiet":sget("quiet"),"calm":sget("calm"),
                "learned":len(mem.get("learned",[])),"gps":g,
                "caretaker_enabled": bool(mem.get("caretaker",{}).get("enabled",True))
            })
        if u.path=="/api/suggest":
            return self._send(200,"application/json; charset=utf-8",{"suggestions": suggestions()})
        if u.path=="/api/learned":
            try: n=int(parse_qs(u.query).get("n",["10"])[0])
            except Exception: n=10
            items=mem.get("learned",[]); last=items[-n:] if n>0 else []
            return self._send(200,"application/json; charset=utf-8",{"items": last, "total": len(items)})
        return self._send(404,body="Not found")
    def do_POST(self):
        global LAST_CMD_TS
        u=urlparse(self.path)
        if u.path!="/api/cmd": return self._send(404,body="Not found")
        ln=int(self.headers.get("Content-Length","0") or 0)
        raw=self.rfile.read(ln).decode("utf-8") if ln else "{}"
        try: payload=json.loads(raw)
        except Exception: payload={}
        cmd=(payload.get("cmd") or "").strip()
        LAST_CMD_TS = time.time()
        import builtins; out=[]; _p=builtins.print
        def cap(*a,**k): out.append(" ".join(str(x) for x in a)); _p(*a,**k)
        builtins.print=cap
        try: handle_command(cmd)
        except SystemExit: out.append("(ignored SystemExit from web)")
        except Exception as e: out.append(f"Error: {e}")
        finally: builtins.print=_p
        return self._send(200,"application/json; charset=utf-8",{"ok":True,"output":"\n".join(out)})

def start_webui():
    def _run():
        try:
            httpd=HTTPServer((WEB_HOST,WEB_PORT),_BNHandler)
            log(f"Web UI: http://{WEB_HOST}:{WEB_PORT}"); httpd.serve_forever()
        except Exception as e: log(f"[!] Web UI error: {e}")
    threading.Thread(target=_run, daemon=True).start()

# ---------- commands ----------
HELP_TEXT = """
Commands:
  say <text> | remember <text> | learned [n] | study <topic> | study on|off | bones on|off
  phase <name> | vehicle <year> <make> <model>
  status | where | save | selfcheck | suggest
  quiet on|off | calm on|off | quiet hours <start> <end> | panic save
  caretaker on|off | caretaker interval <min> | caretaker idle <min> | caretaker auto_blackout on|off
  caretaker add say <text> | caretaker add cmd <command> | caretaker list | caretaker clear
  sleep now | sleep in <secs> | sleep force | sleep cancel | wake at HH:MM | wake
  blackout start | blackout stop | blackout force
  blackout set <minutes|reminder|posture|file|volume> <value>
  help | exit | quit
"""

def vehicle_set_from_parts(parts):
    year = parts[0] if parts and parts[0].isdigit() else None
    rest = parts[1:] if year else parts
    make = rest[0] if rest else None
    model = " ".join(rest[1:]) if len(rest)>1 else None
    mem["vehicle"]={"year":year,"make":make,"model":model}
    wal_log(f"vehicle:{year or ''} {make or ''} {model or ''}".strip()); mark_dirty(); print("Vehicle set:", vehicle_string())

def selfcheck():
    issues=[]
    for p in [STATE_DIR, LOGS_DIR, OUTBOX_DIR, REC_DIR]:
        if not os.path.isdir(p): issues.append(f"missing dir:{p}")
    if not os.path.isfile(MEM_PATH): issues.append("missing memory.json")
    print("Selfcheck:", "OK" if not issues else "; ".join(issues))

def handle_command(cmd: str):
    global SLEEP_ACTIVE, WAKE_AT, BLACKOUT_ACTIVE, LAST_CMD_TS
    LAST_CMD_TS = time.time()
    c=(cmd or "").strip()
    if not c: return
    low=c.lower()

    if low in ("help","?"): print(HELP_TEXT); return
    if low.startswith("say "):
        msg=c[4:].strip(); say(msg); print(msg); return
    if low.startswith("remember "):
        fact=c[9:].strip(); mem.setdefault("facts",{})[f"note@{utc_iso()}"]=fact
        wal_log(f"remember:{fact}"); mark_dirty(); print("OK, stored."); return
    if low.startswith("study "): learn(f"studied:{c[6:].strip()}"); print("Studying…"); return
    if low=="study on":  sset("enable_classroom",True); sset("enable_autostudy",True); print("Study threads ON."); return
    if low=="study off": sset("enable_classroom",False); sset("enable_autostudy",False); print("Study threads OFF."); return
    if low.startswith("phase "):
        name=c[6:].strip() or "Idle"; mem["phases"]["current"]=name; wal_log(f"phase:{name}"); mark_dirty(); print(f"Phase set -> {name}"); return
    if low.startswith("vehicle "): vehicle_set_from_parts(c.split()[1:]); return
    if low=="status":
        cur=mem.get("phases",{}).get("current","Idle")
        print(f"Current phase: {cur}"); print(f"Learned items: {len(mem.get('learned',[]))}")
        print(f"Facts: {len(mem.get('facts',{}))}"); print(f"Deaths: {mem.get('death_count',0)}")
        print(f"Vehicle: {vehicle_string()}"); return
    if low=="where":
        v=vehicle_string(); g=mem.get("last_gps")
        print(f"Vehicle: {v}")
        if g: print(f"GPS: lat {g['lat']:.5f}, lon {g['lon']:.5f}, speed {g['speed_kph']:.1f} kph, heading {g['heading']:.0f}°, ts {g['ts']}")
        else: print("GPS: (no data yet — write state/gps_source.json)")
        return
    if low in ("save","sync"): save_memory(); print("Saved."); return
    if low=="selfcheck": selfcheck(); return
    if low=="suggest":
        for line in suggestions(): print("-", line); return
    if low.startswith("learned"):
        try: n=int(c.split()[1])
        except Exception: n=10
        items=mem.get("learned",[]); tail=items[-n:] if n>0 else []
        if not tail: print("(no learned items yet)")
        else:
            for it in tail: print("-", it)
        return

    if low=="bones on":  sset("bones_mode",True);  print("Bones mode ON. “I’m a doctor, not a mechanic.”"); return
    if low=="bones off": sset("bones_mode",False); print("Bones mode OFF."); return

    if low=="quiet on":  sset("quiet",True);  print("Quiet mode ON."); return
    if low=="quiet off": sset("quiet",False); print("Quiet mode OFF."); return
    if low=="calm on":   sset("calm",True);   print("Calm mode ON."); return
    if low=="calm off":  sset("calm",False);  print("Calm mode OFF."); return
    if low.startswith("quiet hours "):
        try:
            _,a,b=c.split(); mem["quiet_hours"]["start"]=int(a); mem["quiet_hours"]["end"]=int(b)
            mark_dirty(); save_memory(); print("Quiet hours set:", mem["quiet_hours"])
        except Exception: print("Usage: quiet hours <startHour> <endHour> (24h)")
        return
    if low=="panic save": save_memory(); print("Saved."); print("Snapshot:", panic_snapshot()); return

    # caretaker controls
    if low == "caretaker on":
        mem.setdefault("caretaker", {})["enabled"] = True; mark_dirty(); save_memory(); print("Caretaker: ON"); return
    if low == "caretaker off":
        mem.setdefault("caretaker", {})["enabled"] = False; mark_dirty(); save_memory(); print("Caretaker: OFF"); return
    if low.startswith("caretaker interval "):
        try:
            m=int(c.split()[-1]); mem.setdefault("caretaker", {})["interval_minutes"]=max(1,m)
            mark_dirty(); save_memory(); print(f"Caretaker interval set to {m} min")
        except Exception: print("Usage: caretaker interval <minutes>")
        return
    if low.startswith("caretaker idle "):
        try:
            m=int(c.split()[-1]); mem.setdefault("caretaker", {})["inactivity_minutes"]=max(1,m)
            mark_dirty(); save_memory(); print(f"Caretaker idle threshold set to {m} min")
        except Exception: print("Usage: caretaker idle <minutes>")
        return
    if low.startswith("caretaker auto_blackout "):
        flag = c.split()[-1].lower() in ("on","true","1","yes")
        mem.setdefault("caretaker", {})["auto_on_blackout"]=flag
        mark_dirty(); save_memory(); print(f"Caretaker auto_on_blackout = {flag}")
        return
    if low.startswith("caretaker add say "):
        text=c[len("caretaker add say "):].strip()
        ct=mem.setdefault("caretaker", {}); ct.setdefault("program", []).append({"type":"say","text":text})
        mark_dirty(); save_memory(); print("Added say-line."); return
    if low.startswith("caretaker add cmd "):
        cmdtxt=c[len("caretaker add cmd "):].strip()
        ct=mem.setdefault("caretaker", {}); ct.setdefault("program", []).append({"type":"cmd","cmd":cmdtxt})
        mark_dirty(); save_memory(); print("Added cmd-step."); return
    if low == "caretaker list":
        ct=mem.setdefault("caretaker", {}); prog=ct.get("program", [])
        if not prog: print("(empty)")
        else:
            for i, st in enumerate(prog, 1):
                if st.get("type")=="say": print(f"{i}. say: {st.get('text','')}")
                else: print(f"{i}. cmd: {st.get('cmd','')}")
        return
    if low == "caretaker clear":
        mem.setdefault("caretaker", {})["program"] = []; mark_dirty(); save_memory(); print("Caretaker program cleared."); return

    # sleep controls
    if low=="sleep now": threading.Thread(target=sleep_routine, kwargs={"countdown_sec":30}, daemon=True).start(); return
    if low.startswith("sleep in "):
        try: secs=int(c.split()[-1])
        except Exception: secs=60
        threading.Thread(target=sleep_routine, kwargs={"countdown_sec":secs}, daemon=True).start(); return
    if low=="sleep force": threading.Thread(target=sleep_routine, kwargs={"countdown_sec":15}, daemon=True).start(); return
    if low in ("sleep cancel","cancel sleep"):
        SLEEP_ACTIVE=False; mem["phases"]["current"]="Idle"; wal_log("phase:Idle"); mark_dirty(); say("Sleep cancelled."); return
    if low.startswith("wake at "):
        hhmm=parse_hhmm(c.split()[-1])
        if hhmm:
            global WAKE_AT; WAKE_AT=hhmm; say(f"Wake alarm set for {hhmm[0]:02d}:{hhmm[1]:02d}.")
        else: print("Usage: wake at HH:MM")
        return
    if low in ("wake","wake now"):
        SLEEP_ACTIVE=False; WAKE_AT=None; mem["phases"]["current"]="Idle"; wal_log("phase:Idle"); mark_dirty(); say("Wake now."); return

    # blackout controls
    if low=="blackout start": threading.Thread(target=blackout_sequence, daemon=True).start(); return
    if low=="blackout force": threading.Thread(target=blackout_sequence, daemon=True).start(); return
    if low=="blackout stop":
        BLACKOUT_ACTIVE=False; request_player("stop"); mem["phases"]["current"]="Idle"; wal_log("phase:Idle"); mark_dirty(); say("Blackout stopped."); return
    if low.startswith("blackout set "):
        parts=c.split()
        try:
            key,val=parts[2]," ".join(parts[3:]); b=mem.setdefault("blackout",{})
            if key=="minutes": b["minutes"]=int(val)
            elif key in ("reminder","reminder_minutes"): b["reminder_minutes"]=int(val)
            elif key=="posture": b["posture"]=val.strip().lower()
            elif key=="file": b["audio_file"]=val.strip()
            elif key=="volume": b["volume"]=float(val)
            else: print("Keys: minutes | reminder | posture | file | volume"); return
            mark_dirty(); save_memory(); print("Blackout config:", mem["blackout"])
        except Exception: print("Usage: blackout set <minutes|reminder|posture|file|volume> <value>")
        return

    if low in ("exit","quit"): raise SystemExit
    print("Unknown command. Type 'help'.")

# ---------- boot-time helpers ----------
def load_config_vehicle():
    if not os.path.exists(CONFIG_PATH): return
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        veh = cfg.get("vehicle") or {}
        if any(veh.get(k) for k in ("make","model","year")):
            mem["vehicle"].update({
                "make": veh.get("make", mem["vehicle"]["make"]),
                "model": veh.get("model", mem["vehicle"]["model"]),
                "year": veh.get("year", mem["vehicle"]["year"]),
            })
            mark_dirty()
    except Exception: pass

def start_webui():
    def _run():
        try:
            httpd=HTTPServer((WEB_HOST,WEB_PORT),_BNHandler)
            log(f"Web UI: http://{WEB_HOST}:{WEB_PORT}"); httpd.serve_forever()
        except Exception as e: log(f"[!] Web UI error: {e}")
    threading.Thread(target=_run, daemon=True).start()

# ---------- main ----------
def main():
    stale = cleanup_stale_lock()
    if stale:
        mem["death_count"] = int(mem.get("death_count",0)) + 1
        wal_log("death:1"); mark_dirty()
    write_lock()

    load_config_vehicle()

    log("Phase persistence active.")
    log(f"Current phase: {mem.get('phases',{}).get('current','Idle')}")

    # start workers
    threading.Thread(target=autosave_loop,      daemon=True).start()
    threading.Thread(target=hourly_backup_loop, daemon=True).start()
    threading.Thread(target=gps_loop,           daemon=True).start()
    threading.Thread(target=wake_scheduler_loop,daemon=True).start()
    threading.Thread(target=trigger_loop,       daemon=True).start()
    threading.Thread(target=classroom_thread,   daemon=True).start()
    threading.Thread(target=autostudy_thread,   daemon=True).start()
    threading.Thread(target=caretaker_loop,     daemon=True).start()

    start_webui()
    say(boot_snark())
    try:
        if sget("bones_mode"):
            say("I’m a doctor, not a mechanic.")
    except Exception: pass

    log(f"{APP_NAME} is running. Type 'help' for commands.")

    try:
        while True:
            line = input("> ")
            handle_command(line)
    except (KeyboardInterrupt, EOFError, SystemExit):
        log("Shutting down…")
    finally:
        try: save_memory()
        except Exception as e: log(f"[!] Final save error: {e}")
        remove_lock()
        log("Goodbye.")

if __name__ == "__main__":
    main()
