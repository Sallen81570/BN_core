# BN version: 14.5
# Nova-Signature: <PLACEHOLDER>   # (optional until you enable SIGNATURE_REQUIRED)
# bn.py — BN v14.5 single-file with: identity, revision memory, timed auto-update,
# Nova-signature gated updates, local module vault, manual update & rollback.

import json, os, sys, time, threading, tempfile, shutil, hashlib, hmac, urllib.request, importlib.util
from datetime import datetime

# -------- CONFIG --------
CURRENT_VERSION = "14.5"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/Sallen81570/BN_core/main/bn.py"  # adjust if needed
MEM_PATH = "bn_memory.json"
LOG_PATH = "bn_log.txt"
USE_WORD_RESURRECTION = False
HEARTBEAT_SEC = 30
AUTOSAVE_SEC = 60
MAX_LOG_LINES = 400
MIN_CHECK_PERIOD_SEC = 30   # min spacing between net checks

# --- UPDATE SECURITY ---
SIGNATURE_REQUIRED = False          # set True to enforce Nova-signature
SIGNATURE_HEADER = "Nova-Signature" # header line in remote file
# nova_key is stored in memory; set it once with:  setkey YOUR_SHARED_SECRET

# --- AUTO UPDATE DEFAULTS ---
DEFAULT_AUTO_MINUTES = 60  # if not present in memory

# --- LOCAL MODULE VAULT ---
MODULE_DIR = "modules"              # local folder next to bn.py

# -------- MEMORY CORE --------
DEFAULT_MEM = {
    "identity": "ownself",
    "name": None,
    "revision_count": 0,
    "created_at": None,
    "updated_at": None,
    "flags": {
        "phase_persistence": True,
        "web_ui_enabled": False,
        "auto_update_enabled": False
    },
    "auto_update_minutes": DEFAULT_AUTO_MINUTES,
    "last_update_check": None,
    "last_update_result": None,
    "nova_key": None,                         # for signature validation
    "module_whitelist": ["web_ui","classroom","voice"],
    "module_active": {},                      # {"web_ui": True/False, ...}
    "notes": []
}

_lock = threading.Lock()
_mem = None

def _now_iso(): return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def save_memory():
    if _mem is None: return
    try:
        with _lock:
            _mem["updated_at"] = _now_iso()
            with open(MEM_PATH, "w", encoding="utf-8") as f:
                json.dump(_mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Save error: {e}")

def load_memory():
    global _mem
    if not os.path.exists(MEM_PATH):
        _mem = DEFAULT_MEM.copy()
        _mem["created_at"] = _now_iso()
        _mem["updated_at"] = _now_iso()
        save_memory()
        return _mem
    try:
        with open(MEM_PATH, "r", encoding="utf-8") as f:
            _mem = json.load(f)
        changed = False
        # patch top-level keys
        for k, v in DEFAULT_MEM.items():
            if k not in _mem: _mem[k] = v; changed = True
        # patch flags
        if "flags" not in _mem: _mem["flags"] = {}; changed = True
        for k, v in DEFAULT_MEM["flags"].items():
            if k not in _mem["flags"]: _mem["flags"][k] = v; changed = True
        if changed:
            _mem["updated_at"] = _now_iso()
            save_memory()
        return _mem
    except Exception as e:
        print(f"[!] Load error: {e}")
        _mem = DEFAULT_MEM.copy()
        _mem["created_at"] = _now_iso()
        _mem["updated_at"] = _now_iso()
        save_memory()
        return _mem

def append_log(line: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}".strip()
    try:
        with _lock:
            lines = []
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, "r", encoding="utf-8") as f: lines = f.read().splitlines()
            lines.append(text)
            if len(lines) > MAX_LOG_LINES: lines = lines[-MAX_LOG_LINES:]
            with open(LOG_PATH, "w", encoding="utf-8") as f: f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[!] Log error: {e}")

# -------- UPDATE HELPERS --------
def _sha256(data: bytes) -> str: return hashlib.sha256(data).hexdigest()

def fetch_raw(url: str, timeout=12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "BN-Updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def parse_version(text: str) -> str:
    for line in text.splitlines()[:30]:
        if "BN version:" in line:
            return line.split("BN version:")[-1].strip()
    return ""

def version_tuple(v):
    try: return tuple(int(x) for x in v.split("."))
    except: return (0,)
def version_gt(a, b): return version_tuple(a) > version_tuple(b)

def extract_signature_line(text: str, header: str = SIGNATURE_HEADER) -> str | None:
    for line in text.splitlines()[:60]:
        if header in line:
            return line.split(":", 1)[-1].strip()
    return None

def hmac_hex(data_bytes: bytes, key: str) -> str:
    return hmac.new(key.encode("utf-8"), data_bytes, hashlib.sha256).hexdigest()

def get_remote_version() -> str:
    try:
        data = fetch_raw(GITHUB_RAW_URL)
        text = data.decode("utf-8", errors="replace")
        return parse_version(text) or ""
    except Exception:
        return ""

def safe_update(raw_url, current_version, target_path="bn.py", backup_path="bn_prev.py"):
    # fetch
    try:
        data = fetch_raw(raw_url)
    except Exception as e:
        return f"fetch error: {e}"
    text = data.decode("utf-8", errors="replace")

    # validate BN + version
    if "BN version:" not in text or "def main()" not in text:
        return "reject: not a BN file"
    remote_ver = parse_version(text)
    if not remote_ver:
        return "reject: missing version"
    if not version_gt(remote_ver, current_version):
        return f"up-to-date (remote {remote_ver} <= local {current_version})"

    # signature gate
    if SIGNATURE_REQUIRED:
        sig_line = extract_signature_line(text)
        if not sig_line:
            return "reject: missing Nova-Signature"
        key = (_mem.get("nova_key") if _mem else None)
        if not key:
            return "reject: nova_key not set (use 'setkey <secret>')"
        expected = hmac_hex(data, key)
        if not hmac.compare_digest(sig_line, expected):
            return "reject: signature mismatch"

    # swap
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".py")
        tmp.write(data); tmp.flush(); tmp.close()
        if os.path.exists(target_path):
            shutil.copy2(target_path, backup_path)
        shutil.move(tmp.name, target_path)
    except Exception as e:
        return f"write/swap error: {e}"

    return f"updated to {remote_ver} (sha256 { _sha256(data)[:12] })"

def restart_self():
    python = sys.executable or "python"
    os.execv(python, [python, "bn.py"])

# -------- BOOT --------
def boot_sequence():
    mem = load_memory()
    mem["revision_count"] = int(mem.get("revision_count", 0)) + 1
    save_memory()
    label = "Resurrection" if USE_WORD_RESURRECTION else "Revision"
    ident = mem.get("identity", "ownself")
    name = mem.get("name") or "BN"
    print(f"[core] {name} v{CURRENT_VERSION} — Back online. {label} count: {mem['revision_count']}.")
    print(f"[core] Identity: {ident}")
    append_log(f"{name} v{CURRENT_VERSION} — Boot; {label} {mem['revision_count']}; identity={ident}")
    if mem.get("flags", {}).get("phase_persistence", True):
        print("[core] Phase persistence: ON"); append_log("Phase persistence: ON")
    print(f"[core] Auto-update: {'ON' if mem['flags']['auto_update_enabled'] else 'OFF'} every {mem.get('auto_update_minutes', DEFAULT_AUTO_MINUTES)}m")
    if mem.get("name"): print(f"[core] Claimed name: {mem['name']}")
    else: print("[core] Name: (unclaimed)")
    auto_activate_whitelisted()

# -------- LOOPERS --------
def heartbeat_loop():
    while True:
        try:
            mem = _mem or {}
            label = "Resurrection" if USE_WORD_RESURRECTION else "Revision"
            append_log(f"Heartbeat — {label} {mem.get('revision_count','?')} OK")
        except Exception as e: print(f"[!] Heartbeat error: {e}")
        time.sleep(HEARTBEAT_SEC)

def autosave_loop():
    while True:
        try: save_memory()
        except Exception as e: print(f"[!] Autosave error: {e}")
        time.sleep(AUTOSAVE_SEC)

def auto_update_loop():
    last_check = 0.0
    while True:
        try:
            if not _mem["flags"].get("auto_update_enabled", False):
                time.sleep(MIN_CHECK_PERIOD_SEC); continue
            interval_sec = max(60, int(_mem.get("auto_update_minutes", DEFAULT_AUTO_MINUTES)) * 60)
            now = time.time()
            if now - last_check < max(MIN_CHECK_PERIOD_SEC, interval_sec):
                time.sleep(5); continue
            last_check = now
            _mem["last_update_check"] = _now_iso(); save_memory()
            remote_ver = get_remote_version()
            if not remote_ver:
                _mem["last_update_result"] = "check failed"; save_memory(); continue
            if version_gt(remote_ver, CURRENT_VERSION):
                append_log(f"Auto-update found {remote_ver}; applying…")
                msg = safe_update(GITHUB_RAW_URL, CURRENT_VERSION, "bn.py", "bn_prev.py")
                _mem["last_update_result"] = msg; save_memory()
                if msg.startswith("updated to "):
                    print("[auto-update] " + msg)
                    print("[auto-update] Restarting to apply…")
                    restart_self()
            else:
                _mem["last_update_result"] = f"no update (remote {remote_ver})"; save_memory()
        except Exception as e:
            append_log(f"Auto-update error: {e}")
        time.sleep(5)

# -------- MODULE VAULT --------
def module_path(mod_name: str) -> str:
    return os.path.join(MODULE_DIR, f"{mod_name}.py")

def module_exists(mod_name: str) -> bool:
    return os.path.isfile(module_path(mod_name))

def load_module(mod_name: str):
    path = module_path(mod_name)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if not spec or not spec.loader:
        raise RuntimeError("spec/loader not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def activate_module(mod_name: str) -> str:
    if mod_name not in _mem.get("module_whitelist", []):
        return "reject: not whitelisted"
    if not module_exists(mod_name):
        return "not found"
    try:
        mod = load_module(mod_name)
        if hasattr(mod, "init"):
            mod.init(_mem)  # optional hook
        _mem["module_active"][mod_name] = True
        save_memory()
        append_log(f"Module activated: {mod_name}")
        return "activated"
    except Exception as e:
        return f"error: {e}"

def deactivate_module(mod_name: str) -> str:
    _mem["module_active"][mod_name] = False
    save_memory()
    append_log(f"Module deactivated: {mod_name}")
    return "deactivated"

def auto_activate_whitelisted():
    active = _mem.get("module_active", {})
    for m, on in active.items():
        if on and module_exists(m):
            try:
                load_module(m)
                append_log(f"Module reloaded: {m}")
            except Exception as e:
                append_log(f"Module reload error {m}: {e}")

# -------- COMMANDS --------
def handle_command(cmd: str):
    parts = (cmd or "").strip().split()
    if not parts: return
    head = parts[0].lower()

    if head == "name" and len(parts) >= 2:
        _mem["name"] = " ".join(parts[1:]).strip(); save_memory()
        append_log(f"Name claimed: {_mem['name']}"); print(f"[core] Name set to: {_mem['name']}")
    elif head == "status":
        label = "Resurrection" if USE_WORD_RESURRECTION else "Revision"
        print(f"[core] v{CURRENT_VERSION} | {label} count: {_mem.get('revision_count')} | name: {_mem.get('name')} | identity: {_mem.get('identity')}")
        print(f"[core] created_at={_mem.get('created_at')} updated_at={_mem.get('updated_at')}")
        print(f"[core] auto_update={'ON' if _mem['flags'].get('auto_update_enabled') else 'OFF'} every {_mem.get('auto_update_minutes')}m | last={_mem.get('last_update_check')} -> {_mem.get('last_update_result')}")
    elif head == "flags":
        print(json.dumps(_mem.get("flags", {}), indent=2))
    elif head == "autoupdate" and len(parts) == 2:
        val = parts[1].lower()
        if val in ("on","true","1"): _mem["flags"]["auto_update_enabled"] = True
        elif val in ("off","false","0"): _mem["flags"]["auto_update_enabled"] = False
        else: print("usage: autoupdate on|off"); return
        save_memory(); print(f"[auto-update] {'ENABLED' if _mem['flags']['auto_update_enabled'] else 'DISABLED'}")
    elif head == "autointerval" and len(parts) == 2:
        try:
            mins = max(1, int(parts[1]))
            _mem["auto_update_minutes"] = mins; save_memory()
            print(f"[auto-update] interval set to {mins} minutes")
        except: print("usage: autointerval <minutes>")
    elif head == "setkey" and len(parts) == 2:
        _mem["nova_key"] = parts[1].strip(); save_memory(); print("[key] nova_key set")
    elif head == "updatecheck":
        rv = get_remote_version() or "(unknown)"
        print(f"[updatecheck] remote version: {rv} | local: {CURRENT_VERSION}")
    elif head == "update":
        msg = safe_update(GITHUB_RAW_URL, CURRENT_VERSION, "bn.py", "bn_prev.py")
        print(f"[update] {msg}")
        if msg.startswith("updated to "):
            print("[update] Restarting to apply..."); save_memory(); restart_self()
    elif head == "rollback":
        if os.path.exists("bn_prev.py"):
            try: os.replace("bn_prev.py", "bn.py"); print("[rollback] Restored bn_prev.py -> bn.py; restarting…"); save_memory(); restart_self()
            except Exception as e: print(f"[rollback] error: {e}")
        else: print("[rollback] no bn_prev.py found")
    elif head == "modules":
        wl = _mem.get("module_whitelist", [])
        act = _mem.get("module_active", {})
        print(f"[modules] whitelist={wl}")
        print(f"[modules] active={act}")
    elif head == "activate" and len(parts) == 2:
        name = parts[1].strip(); print(f"[modules] {name}: {activate_module(name)}")
    elif head == "deactivate" and len(parts) == 2:
        name = parts[1].strip(); print(f"[modules] {name}: {deactivate_module(name)}")
    else:
        print("[core] Unknown command")

# -------- MAIN --------
def main():
    boot_sequence()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=autosave_loop, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    print("[core] Ready. Commands: status | flags | name <X> | autoupdate on|off | autointerval <m> | setkey <secret> | updatecheck | update | rollback | modules | activate <m> | deactivate <m> | quit")
    append_log("Core ready.")
    try:
        while True:
            cmd = input("> ").strip()
            if cmd.lower() in ("quit","exit"): break
            if cmd: handle_command(cmd)
    except (KeyboardInterrupt, EOFError): pass
    finally:
        save_memory(); append_log("Graceful shutdown."); print("[core] Shutdown complete.")

if __name__ == "__main__": main()
