"""
GreenNode AI Agent — Flask Backend
Deploy: gunicorn app:app -w 2 -b 0.0.0.0:8000
"""
import os, requests, json
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from functools import wraps
from datetime import datetime, timedelta
import threading
from dotenv import load_dotenv
from flask import session, redirect, url_for
try:
    import psycopg2
    import psycopg2.extras
    USE_PG = True
except ImportError:
    import sqlite3
    USE_PG = False
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import pytz

load_dotenv()  # load .env file automatically

# ── Admin auth config ─────────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "greennode2025")

# ── Database credential store (PostgreSQL or SQLite fallback) ─────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH      = os.path.join(os.path.dirname(__file__), "credentials.db")

def get_conn():
    """Get database connection — PostgreSQL if available, else SQLite."""
    if USE_PG and DATABASE_URL:
        # Render provides DATABASE_URL starting with postgres:// — fix for psycopg2
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url)
    else:
        import sqlite3 as _sq
        conn = _sq.connect(DB_PATH)
        conn.row_factory = _sq.Row
        return conn

def init_db():
    conn = get_conn()
    cur  = conn.cursor()
    if USE_PG and DATABASE_URL:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id          SERIAL PRIMARY KEY,
                name        TEXT UNIQUE NOT NULL,
                client_id   TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                note        TEXT DEFAULT '',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                client_id   TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                note        TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
    conn.commit()
    conn.close()

init_db()

def get_all_customers():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id,name,client_id,client_secret,project_id,note,created_at FROM customers ORDER BY name")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows

def get_customer(name):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id,name,client_id,client_secret,project_id,note,created_at FROM customers WHERE LOWER(name)=LOWER(%s)" if (USE_PG and DATABASE_URL) else
                "SELECT id,name,client_id,client_secret,project_id,note,created_at FROM customers WHERE LOWER(name)=LOWER(?)", (name,))
    cols = [d[0] for d in cur.description]
    row  = cur.fetchone()
    conn.close()
    return dict(zip(cols, row)) if row else None

def save_customer(name, client_id, client_secret, project_id, note=""):
    conn = get_conn()
    cur  = conn.cursor()
    if USE_PG and DATABASE_URL:
        cur.execute("""
            INSERT INTO customers (name, client_id, client_secret, project_id, note)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(name) DO UPDATE SET
                client_id=EXCLUDED.client_id,
                client_secret=EXCLUDED.client_secret,
                project_id=EXCLUDED.project_id,
                note=EXCLUDED.note
        """, (name, client_id, client_secret, project_id, note))
    else:
        cur.execute("""
            INSERT INTO customers (name, client_id, client_secret, project_id, note)
            VALUES (?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                client_id=excluded.client_id,
                client_secret=excluded.client_secret,
                project_id=excluded.project_id,
                note=excluded.note
        """, (name, client_id, client_secret, project_id, note))
    conn.commit()
    conn.close()

def delete_customer(name):
    conn = get_conn()
    cur  = conn.cursor()
    ph   = "%s" if (USE_PG and DATABASE_URL) else "?"
    cur.execute(f"DELETE FROM customers WHERE LOWER(name)=LOWER({ph})", (name,))
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected > 0

app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-prod")

# Fix for running behind proxy (nginx)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Only use Secure cookies if running on HTTPS
IS_HTTPS = os.getenv("HTTPS_ENABLED", "false").lower() == "true"
app.config.update(
    SESSION_COOKIE_SECURE=IS_HTTPS,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
CORS(app, supports_credentials=True)

# ── Global error handlers ─────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Not found"}), 404
    return redirect('/login')

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": str(e)}), 500
    raise e

# ── Admin authentication ───────────────────────────────────────────────────────
def admin_required(f):
    """Check session OR Authorization header token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check session
        if session.get("admin_logged_in"):
            return f(*args, **kwargs)
        # Check Authorization header (Bearer token)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token == make_admin_token():
                return f(*args, **kwargs)
        # Check X-Admin-Token header
        token = request.headers.get("X-Admin-Token", "")
        if token and token == make_admin_token():
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized", "redirect": "/login"}), 401
        return redirect("/login")
    return decorated

# ── Scheduler setup ───────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(
    jobstores={'default': MemoryJobStore()},
    timezone=pytz.timezone('Asia/Ho_Chi_Minh')
)
scheduler.start()
_scheduled_jobs = {}  # job_id → {desc, action, params, creds, run_time}

@app.after_request
def add_headers(response):
    # Allow Teams to load in iframe + skip ngrok browser warning
    response.headers['ngrok-skip-browser-warning'] = 'true'
    response.headers['X-Frame-Options'] = 'ALLOWALL'
    response.headers['Content-Security-Policy'] = "frame-ancestors *"
    return response

# ── Config từ .env ────────────────────────────────────────────────────────────
GN_MAAS_API_KEY     = os.getenv("GN_MAAS_API_KEY", "")
GN_MAAS_URL         = "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1/chat/completions"
GN_MAAS_MODEL       = os.getenv("GN_MAAS_MODEL", "google/gemma-4-31b-it")
GN_TOKEN_URL        = "https://iamapis.vngcloud.vn/accounts-api/v2/auth/token"
GN_USERINFO_URL     = "https://iamapis.vngcloud.vn/accounts-api/v1/auth/userinfo"
GN_API_BASE         = "https://hcm-3.api.vngcloud.vn/vserver/vserver-gateway"

# ── Token cache (in-memory, thread-safe) ─────────────────────────────────────
_token_cache = {}   # key: client_id → {token, expires_at, user_info}
_cache_lock  = threading.Lock()

def get_cached_token(client_id):
    with _cache_lock:
        entry = _token_cache.get(client_id)
        if entry and datetime.utcnow() < entry["expires_at"]:
            return entry
        return None

def set_cached_token(client_id, token, expires_in, user_info):
    with _cache_lock:
        _token_cache[client_id] = {
            "token":      token,
            "user_info":  user_info,
            "expires_at": datetime.utcnow() + timedelta(seconds=expires_in - 60)
        }

def fetch_gn_token(client_id, client_secret):
    """Fetch GreenNode access token using client credentials."""
    cached = get_cached_token(client_id)
    if cached:
        return cached["token"], cached["user_info"]

    import base64
    b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(GN_TOKEN_URL,
        headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=email",
        verify=False, timeout=15)
    r.raise_for_status()
    data = r.json()
    token      = data.get("access_token") or data.get("accessToken")
    expires_in = data.get("expires_in", 1800)
    if not token:
        raise ValueError(f"No access_token in response: {data}")

    # Get userinfo
    u = requests.get(GN_USERINFO_URL,
        headers={"Authorization": f"Bearer {token}"},
        verify=False, timeout=10)
    user_info = u.json() if u.ok else {}
    print(f"[USERINFO] status={u.status_code} userId={user_info.get('userId','')} accountId={user_info.get('accountId','')} keys={list(user_info.keys())}")

    set_cached_token(client_id, token, expires_in, user_info)
    return token, user_info

def gn_api(token, user_id, method, path, body=None):
    """Call GreenNode vServer API."""
    url = f"{GN_API_BASE}/{path}"
    headers = {
        "Authorization":    f"Bearer {token}",
        "Content-Type":     "application/json",
        "portal-user-id":   str(user_id),
        "x-portal-user-id": str(user_id),
    }
    r = requests.request(method, url, headers=headers,
                         json=body, verify=False, timeout=20)
    return r.status_code, r.json() if r.text else {}


# ── Customer credential CRUD ──────────────────────────────────────────────────
@app.route("/api/customers", methods=["GET"])
@admin_required
def list_customers():
    customers = get_all_customers()
    # Don't expose secrets
    safe = [{
        "id":         c["id"],
        "name":       c["name"],
        "project_id": c["project_id"],
        "note":       c["note"],
        "created_at": c["created_at"],
        "clientId":   c["client_id"][:8] + "****",  # mask
    } for c in customers]
    return jsonify({"customers": safe, "count": len(safe)})

@app.route("/api/customers", methods=["POST"])
@admin_required
def add_customer():
    body = request.get_json() or {}
    name          = body.get("name", "").strip()
    client_id     = body.get("clientId", "").strip()
    client_secret = body.get("clientSecret", "").strip()
    project_id    = body.get("projectId", "").strip()
    note          = body.get("note", "").strip()
    if not all([name, client_id, client_secret, project_id]):
        return jsonify({"error": "Cần điền: name, clientId, clientSecret, projectId"}), 400
    # Validate credentials
    try:
        fetch_gn_token(client_id, client_secret)
    except Exception as e:
        return jsonify({"error": f"Credentials không hợp lệ: {e}"}), 400
    save_customer(name, client_id, client_secret, project_id, note)
    return jsonify({"ok": True, "message": f"✅ Đã lưu credentials cho '{name}'"})

@app.route("/api/customers/<name>", methods=["DELETE"])
@admin_required
def remove_customer(name):
    if delete_customer(name):
        return jsonify({"ok": True, "message": f"Đã xóa '{name}'"})
    return jsonify({"error": f"Không tìm thấy '{name}'"}), 404

# ── Auth endpoint ─────────────────────────────────────────────────────────────
@app.route("/api/auth", methods=["POST"])
def auth():
    """Validate credentials and return user info."""
    body = request.get_json() or {}
    client_id     = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id    = body.get("projectId", "")
    if not client_id or not client_secret:
        return jsonify({"error": "clientId and clientSecret required"}), 400
    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
        return jsonify({
            "ok":        True,
            "userId":    user_info.get("userId", ""),
            "accountId": user_info.get("accountId", 0),
            "username":  user_info.get("username", ""),
            "email":     user_info.get("rootEmail", ""),
            "projectId": project_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 401

# ── Data endpoint: fetch all resources real-time ──────────────────────────────
@app.route("/api/resources", methods=["POST"])
def resources():
    """Fetch all GreenNode resources real-time (no caching)."""
    body       = request.get_json() or {}
    client_id  = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id = body.get("projectId", "")
    if not client_id or not project_id:
        return jsonify({"error": "clientId and projectId required"}), 400
    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        P   = project_id

        result = {}

        # VM
        status, data = gn_api(token, uid, "GET", f"v2/{P}/servers")
        result["vm"] = data.get("listData", []) if status == 200 else []

        # Volume
        status, data = gn_api(token, uid, "GET", f"v2/{P}/volumes")
        result["volume"] = data.get("listData", []) if status == 200 else []

        # Network
        status, data = gn_api(token, uid, "GET", f"v2/{P}/networks")
        result["network"] = data.get("listData", []) if status == 200 else []

        # Security groups (extract from VMs)
        sg_map = {}
        for s in result["vm"]:
            for sg in s.get("secGroups", []):
                uid_ = sg.get("uuid", sg.get("id", ""))
                if uid_ not in sg_map:
                    sg_map[uid_] = {**sg, "servers": []}
                sg_map[uid_]["servers"].append({"name": s["name"], "id": s["uuid"]})
        result["sg"] = list(sg_map.values())

        # Floating IPs from interfaces
        fips = []
        for s in result["vm"]:
            for iface in s.get("internalInterfaces", []):
                if iface.get("floatingIp"):
                    fips.append({
                        "ip":         iface["floatingIp"],
                        "id":         iface.get("floatingIpId", ""),
                        "status":     iface.get("status", ""),
                        "serverName": s["name"],
                        "serverId":   s["uuid"],
                        "fixedIp":    iface.get("fixedIp", ""),
                    })
        result["floatingip"] = fips
        result["fetchedAt"]  = datetime.utcnow().isoformat() + "Z"
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Chat endpoint: real-time GN data + Claude ────────────────────────────────
# ── Intent detection helpers ─────────────────────────────────────────────────
def detect_action_intent(message, vms, sgs, volumes=[]):
    """
    Detect if user wants to execute an action.
    Returns (action_type, params, description) or (None, None, None).
    Schedule intents are checked FIRST before immediate actions.
    """
    import re
    from datetime import datetime as dt
    msg = message.lower()

    def find_vm(text):
        text_lower = text.lower()
        # Exact match first
        for vm in vms:
            name = (vm.get("name") or "").lower()
            if name and name in text_lower:
                return vm
        # Partial/fuzzy match — check if any word in text matches part of VM name
        for vm in vms:
            name = (vm.get("name") or "").lower()
            # Remove underscores and compare
            name_clean = name.replace("_", "").replace("-", "")
            text_clean = text_lower.replace("_", "").replace("-", "")
            if name_clean and name_clean in text_clean:
                return vm
            # Check if significant part of name appears in text
            parts = name.replace("_", " ").replace("-", " ").split()
            if any(p in text_lower for p in parts if len(p) > 3):
                return vm
        # If only 1 VM, return it
        return vms[0] if len(vms) == 1 else None

    def find_sg(text):
        for sg in sgs:
            name = (sg.get("name") or "").lower()
            if name and name in text:
                return sg
        return None

    # ── List/cancel schedule ─────────────────────────────────────────────────
    if any(w in msg for w in ["xem lịch", "danh sách lịch", "lịch hẹn", "lịch đã đặt", "đang hẹn"]):
        return ("list_schedule", {}, "Danh sách lịch hẹn hiện tại")

    if any(w in msg for w in ["hủy lịch", "xóa lịch", "bỏ lịch", "cancel schedule"]):
        return ("cancel_schedule", {}, "Hủy lịch hẹn")

    # ── Schedule intent (MUST check before immediate actions) ────────────────
    SCHEDULE_KEYWORDS = ["hẹn", "đặt lịch", "schedule", "tự động", "vào lúc", "lúc", "hẹn giờ", "hẹn mở", "hẹn tắt", "hẹn bật", "hẹn khởi"]
    has_schedule = any(w in msg for w in SCHEDULE_KEYWORDS)

    # Extract time: 3h30, 03:30, 3 giờ 30, 3:36
    hour, minute = None, None
    time_pats = [
        r'(\d{1,2})h(\d{2})',
        r'(\d{1,2}):(\d{2})',
        r'(\d{1,2})\s*gi[oờ]\s*(\d{2})',
        r'(\d{1,2})h(?!\d)',   # "3h" without minutes → 3:00
    ]
    for pat in time_pats:
        m = re.search(pat, msg)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if len(m.groups()) > 1 and m.group(2) else 0
            break

    # Extract date
    day, month, year = None, None, None
    date_pats = [
        r'ngày\s*(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?',
        r'(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?',
    ]
    for pat in date_pats:
        m = re.search(pat, msg)
        if m:
            g = m.groups()
            day, month = int(g[0]), int(g[1])
            year = int(g[2]) if len(g) > 2 and g[2] else dt.now().year
            break

    if has_schedule and hour is not None:
        # Determine scheduled action
        sched_action = None
        if any(w in msg for w in ["mở", "bật", "start", "khởi động", "khởi"]):
            sched_action = "vm_start"
        elif any(w in msg for w in ["tắt", "dừng", "stop", "shutdown"]):
            sched_action = "vm_stop"

        if sched_action:
            vm = find_vm(msg)
            if vm:
                now_dt   = dt.now()
                run_day   = day   or now_dt.day
                run_month = month or now_dt.month
                run_year  = year  or now_dt.year
                try:
                    run_time = dt(run_year, run_month, run_day, hour, minute)
                    action_label = "khởi động" if sched_action == "vm_start" else "tắt"
                    return (
                        f"schedule_{sched_action}",
                        {
                            "serverId":    vm.get("uuid"),
                            "serverName":  vm.get("name"),
                            "runAt":       run_time.isoformat(),
                            "schedAction": sched_action,
                        },
                        f"Hẹn lịch **{action_label}** VM **{vm.get('name')}** lúc **{hour:02d}:{minute:02d} ngày {run_day:02d}/{run_month:02d}/{run_year}**"
                    )
                except ValueError:
                    pass
            else:
                return ("schedule_unknown", None, "Bạn muốn hẹn lịch cho VM nào?")

    # ── Immediate actions (only if no schedule keyword) ──────────────────────
    # "tóm tắt" should NOT trigger vm_stop — check it's not part of "tóm tắt"
    has_stop = any(w in msg for w in ["stop", "dừng", "shut", "shutdown"]) or                ("tắt" in msg and "tóm tắt" not in msg and "tóm" not in msg)
    if has_stop:
        if any(w in msg for w in ["vm", "server", "máy"]) or find_vm(msg):
            vm = find_vm(msg)
            if vm:
                return ("vm_stop", {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                        f"Dừng VM **{vm.get('name')}** (ACTIVE → SHUTOFF)")
            return ("vm_stop", None, "Bạn muốn dừng VM nào?")

    has_start = any(w in msg for w in ["start", "khởi động", "turn on"]) or                 (any(w in msg for w in ["bật", "mở"]) and not any(w in msg for w in SCHEDULE_KEYWORDS))
    if has_start:
        if any(w in msg for w in ["vm", "server", "máy"]) or find_vm(msg):
            vm = find_vm(msg)
            if vm:
                return ("vm_start", {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                        f"Khởi động VM **{vm.get('name')}** (SHUTOFF → ACTIVE)")
            return ("vm_start", None, "Bạn muốn khởi động VM nào?")

    if any(w in msg for w in ["reboot", "restart", "khởi động lại", "reset"]):
        if any(w in msg for w in ["vm", "server", "máy"]) or find_vm(msg):
            vm = find_vm(msg)
            if vm:
                return ("vm_reboot", {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                        f"Khởi động lại VM **{vm.get('name')}**")
            return ("vm_reboot", None, "Bạn muốn reboot VM nào?")

    # ── Volume attach/detach ─────────────────────────────────────────────────
    def find_volume(text):
        """Find volume by name (case-insensitive partial match), return volume dict with UUID."""
        for vol in volumes:
            vname = (vol.get("name") or vol.get("volumeName") or "").lower()
            if vname and vname in text.lower():
                return vol
        # Try extracting word after "volume" keyword
        import re as _re2
        m = _re2.search(r'volume\s+([\w\-\.]+)', text.lower())
        if m:
            keyword = m.group(1)
            for vol in volumes:
                vname = (vol.get("name") or vol.get("volumeName") or "").lower()
                if keyword in vname or vname in keyword:
                    return vol
        return None

    if any(w in msg for w in ["gắn volume", "attach volume", "gắn disk"]):
        vm = find_vm(msg)
        vol = find_volume(msg)
        if vm and vol:
            vol_id   = vol.get("uuid") or vol.get("id") or vol.get("volumeId")
            vol_name = vol.get("name") or vol.get("volumeName")
            return ("volume_attach",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"),
                     "volumeId": vol_id, "volumeName": vol_name},
                    f"Gắn volume **{vol_name}** (ID: `{str(vol_id)[:8]}...`) vào VM **{vm.get('name')}**")
        missing = f"tên VM{' ✓' if vm else ' ✗'} và tên Volume{' ✓' if vol else ' ✗'}"
        return ("volume_attach", None, f"Không tìm thấy: {missing}. Hỏi 'liệt kê volume' để xem danh sách.")

    if any(w in msg for w in ["gỡ volume", "detach volume", "tháo disk", "gỡ disk"]):
        vm = find_vm(msg)
        vol = find_volume(msg)
        if vm and vol:
            vol_id   = vol.get("uuid") or vol.get("id") or vol.get("volumeId")
            vol_name = vol.get("name") or vol.get("volumeName")
            return ("volume_detach",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"),
                     "volumeId": vol_id, "volumeName": vol_name},
                    f"Gỡ volume **{vol_name}** khỏi VM **{vm.get('name')}**")
        return ("volume_detach", None, "Không tìm thấy VM hoặc Volume. Hỏi 'liệt kê volume' để xem danh sách.")

    # ── Floating IP ───────────────────────────────────────────────────────────
    if any(w in msg for w in ["gắn floating", "associate ip", "gắn ip công cộng"]):
        vm = find_vm(msg)
        import re as _re
        ip_match = _re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', msg)
        fip = ip_match.group(1) if ip_match else None
        if vm and fip:
            return ("fip_associate",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "floatingIp": fip},
                    f"Gắn Floating IP **{fip}** vào VM **{vm.get('name')}**")
        return ("fip_associate", None, "Cần biết tên VM và địa chỉ Floating IP cần gắn")
    if any(w in msg for w in ["gỡ floating", "disassociate ip", "gỡ ip công cộng"]):
        vm = find_vm(msg)
        if vm:
            return ("fip_disassociate",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                    f"Gỡ Floating IP khỏi VM **{vm.get('name')}**")
        return ("fip_disassociate", None, "Bạn muốn gỡ Floating IP khỏi VM nào?")

    # ── Rename ────────────────────────────────────────────────────────────────
    if any(w in msg for w in ["đổi tên", "rename"]):
        import re
        m = re.search(r'(?:thanh|sang|to|ten)\s+([\w\-\.]+)', msg)
        new_name = m.group(1) if m else None
        vm = find_vm(msg)
        if vm and new_name:
            return ("vm_rename",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "newName": new_name},
                    f"Đổi tên VM **{vm.get('name')}** thành **{new_name}**")
        return ("vm_rename", None, "Bạn muốn đổi tên VM nào thành gì?")

    return (None, None, None)


def execute_vm_action(token, uid, project_id, action_type, params):
    """Execute action and poll for result."""
    import time
    P = project_id
    server_id = params.get("serverId", "")

    ENDPOINT = {
        "vm_stop":   ("PUT",  f"v2/{P}/servers/{server_id}/stop",   None),
        "vm_start":  ("PUT",  f"v2/{P}/servers/{server_id}/start",  None),
        "vm_reboot": ("PUT",  f"v2/{P}/servers/{server_id}/reboot", {"type": "SOFT"}),
    }
    EXPECTED = {"vm_stop": "SHUTOFF", "vm_start": "ACTIVE", "vm_reboot": "ACTIVE"}

    method, path, body = ENDPOINT[action_type]
    status, data = gn_api(token, uid, method, path, body)

    if status >= 300:
        return False, f"API lỗi {status}: {data}", None

    # Poll for actual state change (max 3 minutes)
    expected = EXPECTED[action_type]
    # Intermediate states — keep polling, don't give up
    IN_PROGRESS = {
        "vm_stop":   ("TURNING-OFF", "STOPPING", "SHUTOFF", "POWERED_OFF"),
        "vm_start":  ("STARTING", "BOOTING", "ACTIVE"),
        "vm_reboot": ("REBOOTING", "REBOOT", "STARTING", "ACTIVE"),
    }
    for attempt in range(36):  # 36 x 5s = 3 minutes
        time.sleep(5)
        s2, d2 = gn_api(token, uid, "GET", f"v2/{P}/servers")
        if s2 == 200:
            servers = d2.get("listData", [])
            vm = next((sv for sv in servers if sv.get("uuid") == server_id), None)
            if vm:
                actual = vm.get("status", "UNKNOWN")
                if actual == expected:
                    return True, None, vm
                # Still in transition — keep waiting
                if actual in IN_PROGRESS.get(action_type, []):
                    continue
                # Unexpected error state — stop
                if actual in ("ERROR", "HARD_REBOOT"):
                    return False, f"GreenNode báo lỗi — trạng thái: {actual}", vm

    # Timeout — get final state
    s3, d3 = gn_api(token, uid, "GET", f"v2/{P}/servers")
    if s3 == 200:
        servers = d3.get("listData", [])
        vm = next((sv for sv in servers if sv.get("uuid") == server_id), None)
        if vm:
            actual = vm.get("status", "UNKNOWN")
            # TURNING-OFF / STOPPING = đang xử lý đúng hướng → SUCCESS
            if actual in IN_PROGRESS.get(action_type, []) or actual == expected:
                return True, None, vm
            # Sai trạng thái hoàn toàn → thất bại thật
            return False, f"Trạng thái không mong đợi: {actual}", vm
    # Không lấy được trạng thái — nhưng lệnh đã gửi thành công → SUCCESS
    return True, None, None


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Main chat endpoint.
    1. Fetches fresh GreenNode data for every message.
    2. Detects action intent (stop/start/reboot).
    3. If action confirmed → execute directly via GreenNode API.
    4. Otherwise → ask LLM for answer.
    """
    body          = request.get_json() or {}
    client_id     = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id    = body.get("projectId", "")
    user_message  = body.get("message", "")
    history       = body.get("history", [])
    customer_name = body.get("customerName", "")

    # Load credentials from DB if customerName provided
    if customer_name:
        cust = get_customer(customer_name)
        if cust:
            client_id     = cust["client_id"]
            client_secret = cust["client_secret"]
            project_id    = cust["project_id"]
        else:
            return jsonify({"error": f"Không tìm thấy khách hàng '{customer_name}' trong hệ thống."}), 404

    if not client_id or not project_id or not user_message:
        return jsonify({"error": "Cần clientId+projectId hoặc customerName"}), 400
    if not GN_MAAS_API_KEY:
        return jsonify({"error": "GN_MAAS_API_KEY not configured in .env"}), 500

    # 1. Fetch fresh GN data
    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        P   = project_id

        vms,  volumes,  networks = [], [], []
        s1, d1 = gn_api(token, uid, "GET", f"v2/{P}/servers")
        if s1 == 200: vms = d1.get("listData", [])
        s2, d2 = gn_api(token, uid, "GET", f"v2/{P}/volumes")
        if s2 == 200: volumes = d2.get("listData", [])
        s3, d3 = gn_api(token, uid, "GET", f"v2/{P}/networks")
        if s3 == 200: networks = d3.get("listData", [])

        # SG from VMs
        sg_map = {}
        for s in vms:
            for sg in s.get("secGroups", []):
                k = sg.get("uuid", sg.get("id", ""))
                if k not in sg_map:
                    sg_map[k] = {**sg, "servers": []}
                sg_map[k]["servers"].append(s["name"])
        sgs = list(sg_map.values())

        # Floating IPs
        fips = []
        for s in vms:
            for iface in s.get("internalInterfaces", []):
                if iface.get("floatingIp"):
                    fips.append({"ip": iface["floatingIp"], "server": s["name"], "status": iface.get("status","")})

    except Exception as e:
        return jsonify({"error": f"GreenNode API error: {e}"}), 500

    # 2. Build context
    def fmt_vm(s):
        ip  = s.get("internalInterfaces", [{}])[0].get("fixedIp", "N/A") if s.get("internalInterfaces") else "N/A"
        wan = s.get("internalInterfaces", [{}])[0].get("floatingIp", "N/A") if s.get("internalInterfaces") else "N/A"
        sgs = ", ".join(g.get("name","") for g in s.get("secGroups",[]))
        return (f"VM|{s.get('name')}|{s.get('status')}|private:{ip}|public:{wan}"
                f"|flavor:{s.get('flavor',{}).get('name','?')}"
                f"|os:{s.get('image',{}).get('imageType','?')}"
                f"|zone:{s.get('zoneId','?')}|sgs:[{sgs}]|id:{s.get('uuid')}")

    vm_lines  = "\n".join(fmt_vm(s) for s in vms) or "(none)"
    vol_lines = "\n".join(
        f"VOL|{v.get('name',v.get('volumeName'))}|{v.get('status',v.get('volumeStatus'))}|{v.get('size',v.get('volumeSize'))}GB"
        for v in volumes) or "(none)"
    sg_lines  = "\n".join(
        f"SG|{sg.get('name')}|id:{sg.get('uuid',sg.get('id'))}|attached_to:[{', '.join(sg.get('servers',[]))}]"
        for sg in sgs) or "(none)"
    net_lines = "\n".join(
        f"NET|{n.get('name')}|{n.get('uuid',n.get('id'))}|cidr:{n.get('cidr','?')}"
        for n in networks) or "(none)"
    fip_lines = "\n".join(f"FIP|{f['ip']}|{f['status']}|server:{f['server']}" for f in fips) or "(none)"

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    context = f"""=== REAL-TIME DATA (fetched: {now}) ===
PROJECT: {project_id}
USER: {user_info.get('username','?')} | email: {user_info.get('rootEmail','?')} | type: {user_info.get('userType','?')}

--- VM ({len(vms)}) ---
{vm_lines}

--- Volume ({len(volumes)}) ---
{vol_lines}

--- Security Group ({len(sgs)}) ---
{sg_lines}

--- Network ({len(networks)}) ---
{net_lines}

--- Floating IP ({len(fips)}) ---
{fip_lines}"""

    system_prompt = f"""Bạn là GreenNode AI Assistant — trợ lý quản lý hạ tầng đám mây thông minh cho GreenNode (VNG Cloud) HCM-3.
Dữ liệu bên dưới được lấy REAL-TIME từ GreenNode API ngay lúc user gửi tin nhắn — luôn chính xác và mới nhất.

{context}

HƯỚNG DẪN TRẢ LỜI:
- Trả lời bằng tiếng Việt, ngắn gọn và chính xác
- Dùng Markdown: **bold**, table, bullet list
- Trạng thái VM: 🟢 ACTIVE · 🔴 SHUTOFF · 🟡 BUILD · ⚪ khác
- Phát hiện vấn đề: ⚠️ orphan resource, 🚨 security risk, ❌ lỗi
- Khi user muốn thực hiện action (start/stop/reboot VM, gắn/gỡ SG, tạo snapshot):
  Mô tả rõ action sẽ làm gì và hỏi "Bạn có muốn tiếp tục không?"
  KHÔNG tự động thực hiện — chờ user xác nhận

QUAN TRỌNG — ĐỘ TRỄ TRẠNG THÁI:
GreenNode API nhận lệnh ngay lập tức nhưng việc thực thi thực tế cần 30-120 giây.
Nếu user vừa stop/start/reboot VM và hỏi lại trạng thái ngay:
- Nếu dữ liệu real-time vẫn hiện ACTIVE sau lệnh stop → đây là bình thường, server đang trong quá trình dừng
- KHÔNG nói "đã dừng thành công" nếu dữ liệu thực tế vẫn là ACTIVE
- Hãy nói: "Lệnh đã được gửi. GreenNode đang xử lý — vui lòng chờ 1-2 phút rồi kiểm tra lại"
- Nếu sau 2 phút vẫn không đổi trạng thái → có thể có lỗi, user nên kiểm tra trên portal

DỮ LIỆU REAL-TIME được cập nhật mỗi lần user gửi tin nhắn."""

    # 3. Detect action intent — execute DIRECTLY without asking LLM
    confirmed = body.get("confirmed", False)  # user already confirmed this action
    pending_action = body.get("pendingAction", None)  # {type, params, desc} from previous turn

    if confirmed and pending_action:
        # User confirmed → execute the action NOW
        action_type = pending_action.get("type")
        params      = pending_action.get("params", {})
        desc        = pending_action.get("desc", "")
        server_name = params.get("serverName", "VM")

        if action_type in ("vm_stop", "vm_start", "vm_reboot"):
            ok, err, vm_after = execute_vm_action(token, uid, project_id, action_type, params)
            if ok:
                actual_status = vm_after.get("status", "?") if vm_after else "?"
                # Map trạng thái trung gian sang message thân thiện
                STATUS_MSG = {
                    "SHUTOFF":     ("🔴", "Đã tắt hoàn toàn"),
                    "ACTIVE":      ("🟢", "Đang chạy"),
                    "TURNING-OFF": ("🟡", "Đang tắt — GreenNode đang xử lý, sẽ tắt hoàn toàn trong vài giây"),
                    "STOPPING":    ("🟡", "Đang tắt — GreenNode đang xử lý"),
                    "STARTING":    ("🟡", "Đang khởi động — GreenNode đang xử lý"),
                    "REBOOTING":   ("🟡", "Đang khởi động lại"),
                }
                icon, label = STATUS_MSG.get(actual_status, ("⚪", actual_status))
                if actual_status in ("SHUTOFF", "ACTIVE"):
                    reply = f"✅ **Thành công!** VM **{server_name}**: {icon} {label}"
                else:
                    reply = f"✅ **Lệnh đã thực thi!** VM **{server_name}**: {icon} {label}\n\n*GreenNode sẽ hoàn thành trong 1-2 phút. Hỏi tôi lại để kiểm tra trạng thái.*"
            else:
                reply = f"❌ **Thất bại:** {err}\n\nVui lòng thử lại hoặc kiểm tra trên GreenNode portal."
            return jsonify({"reply": reply, "fetchedAt": now, "actionDone": True})

        # Handle confirmed volume/FIP/SG actions
        EXTENDED_CONFIRM = {"volume_attach","volume_detach","fip_associate","fip_disassociate","sg_attach","sg_detach"}
        if action_type in EXTENDED_CONFIRM:
            ok, data = execute_extended_action(token, uid, project_id, action_type, params)
            labels = {
                "volume_attach":    f"Đã gắn volume **{params.get('volumeName','?')}** vào VM **{params.get('serverName','?')}**",
                "volume_detach":    f"Đã gỡ volume **{params.get('volumeName','?')}** khỏi VM **{params.get('serverName','?')}**",
                "fip_associate":    f"Đã gắn Floating IP **{params.get('floatingIp','?')}** vào VM **{params.get('serverName','?')}**",
                "fip_disassociate": f"Đã gỡ Floating IP khỏi VM **{params.get('serverName','?')}**",
                "sg_attach":        f"Đã gắn Security Group vào VM **{params.get('serverName','?')}**",
                "sg_detach":        f"Đã gỡ Security Group khỏi VM **{params.get('serverName','?')}**",
            }
            if ok:
                reply = f"✅ {labels.get(action_type, 'Thành công!')}"
            else:
                reply = f"❌ Thất bại: {data}"
            return jsonify({"reply": reply, "fetchedAt": now, "actionDone": True})

    # Detect new action intent from this message
    if not confirmed:
        action_type, params, desc = detect_action_intent(user_message, vms, sgs, volumes)
        if action_type and params is not None:
            # Handle schedule intent — execute directly, no confirm needed
            if action_type.startswith("schedule_"):
                sched_action = params.get("schedAction", "")
                server_id    = params.get("serverId", "")
                server_name  = params.get("serverName", "")
                run_at       = params.get("runAt", "")
                try:
                    # Call schedule logic directly — no HTTP self-call
                    result = _do_schedule(
                        client_id, client_secret, project_id,
                        sched_action,
                        {"serverId": server_id, "serverName": server_name},
                        run_at
                    )
                    if not result["ok"]:
                        return jsonify({"reply": f"❌ {result.get('error', 'Lỗi đặt lịch')}", "fetchedAt": now})
                    return jsonify({"reply": result.get("message", "✅ Đã đặt lịch!"), "fetchedAt": now})
                except Exception as e:
                    return jsonify({"reply": f"❌ Lỗi đặt lịch: {e}", "fetchedAt": now})

            # List schedules
            if action_type == "list_schedule":
                if not _scheduled_jobs:
                    return jsonify({"reply": "📅 Hiện không có lịch hẹn nào được đặt.", "fetchedAt": now})
                lines = []
                for jid, job in _scheduled_jobs.items():
                    from datetime import datetime as dt
                    rt = job.get("run_time", "")
                    try:
                        rt_fmt = dt.fromisoformat(rt).strftime("%H:%M ngày %d/%m/%Y")
                    except:
                        rt_fmt = rt
                    action_label = "🟢 Bật" if job["action"] == "vm_start" else "🔴 Tắt"
                    lines.append(f"• {action_label} **{job['params'].get('serverName','')}** lúc **{rt_fmt}** (ID: `{jid}`)")
                reply = f"📅 **Lịch hẹn hiện tại ({len(_scheduled_jobs)}):**\n\n" + "\n".join(lines)
                reply += "\n\nĐể hủy, gõ: **hủy lịch [tên VM]**"
                return jsonify({"reply": reply, "fetchedAt": now})

            # Cancel schedule
            if action_type == "cancel_schedule":
                vm = next((v for v in vms if v.get("name","").lower() in user_message.lower()), None)
                cancelled = []
                for jid in list(_scheduled_jobs.keys()):
                    job = _scheduled_jobs[jid]
                    if not vm or job["params"].get("serverName","").lower() == (vm.get("name","") if vm else "").lower():
                        try:
                            scheduler.remove_job(jid)
                        except:
                            pass
                        cancelled.append(_scheduled_jobs.pop(jid)["desc"])
                if cancelled:
                    return jsonify({"reply": f"✅ Đã hủy {len(cancelled)} lịch:\n" + "\n".join(f"• {c}" for c in cancelled), "fetchedAt": now})
                return jsonify({"reply": "⚠️ Không tìm thấy lịch hẹn nào để hủy.", "fetchedAt": now})

            # Extended actions (volume, FIP, SG, rename) → direct execute via action2
            # Actions requiring confirmation (medium risk)
            CONFIRM_ACTIONS = {"volume_attach","volume_detach","fip_associate","fip_disassociate","sg_attach","sg_detach"}
            if action_type in CONFIRM_ACTIONS and params:
                reply = f"⚠️ **Xác nhận hành động**\n\n{desc}\n\nBạn có chắc muốn thực hiện không? Nhấn nút bên dưới hoặc gõ **xác nhận**."
                return jsonify({
                    "reply": reply, "fetchedAt": now,
                    "needConfirm": True,
                    "pendingAction": {"type": action_type, "params": params, "desc": desc}
                })

            # Actions executed directly (low risk: rename, tag)
            EXTENDED_ACTIONS = {"sg_rule_add","sg_rule_remove","vm_rename","volume_rename"}
            if action_type in EXTENDED_ACTIONS and params:
                ok, data = execute_extended_action(token, uid, project_id, action_type, params)
                if ok:
                    action_labels = {
                        "volume_attach": "Đã gắn volume",
                        "volume_detach": "Đã gỡ volume",
                        "fip_associate": "Đã gắn Floating IP",
                        "fip_disassociate": "Đã gỡ Floating IP",
                        "vm_rename": f"Đã đổi tên VM thành **{params.get('newName','')}**",
                        "volume_rename": f"Đã đổi tên Volume thành **{params.get('newName','')}**",
                    }
                    msg = action_labels.get(action_type, "✅ Thành công")
                    return jsonify({"reply": f"✅ {msg}", "fetchedAt": now, "actionDone": True})
                else:
                    return jsonify({"reply": f"❌ Thất bại: {data}", "fetchedAt": now})

            # Regular action → ask for confirmation
            server_name = params.get("serverName", "")
            reply = f"⚠️ **Xác nhận hành động**\n\n{desc}\n\nBạn có chắc muốn thực hiện không? Nhấn nút bên dưới hoặc gõ **xác nhận**."
            return jsonify({
                "reply":         reply,
                "fetchedAt":     now,
                "needConfirm":   True,
                "pendingAction": {"type": action_type, "params": params, "desc": desc}
            })
        elif action_type and not params:
            return jsonify({"reply": desc, "fetchedAt": now})

    # 4. No action → call GreenNode MaaS LLM
    messages = [{"role": "assistant", "content": system_prompt}]
    messages += list(history[-12:])
    messages += [{"role": "user", "content": user_message}]
    try:
        r = requests.post(
            GN_MAAS_URL,
            headers={
                "Authorization": f"Bearer {GN_MAAS_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":            GN_MAAS_MODEL,
                "messages":         messages,
                "max_tokens":       2000,
                "temperature":      0.7,
                "top_p":            0.9,
                "presence_penalty": 0,
            },
            timeout=60,
            verify=False,
        )
        r.raise_for_status()
        data  = r.json()
        reply = data["choices"][0]["message"]["content"]
        return jsonify({"reply": reply, "fetchedAt": now, "model": GN_MAAS_MODEL})
    except Exception as e:
        return jsonify({"error": f"LLM API error: {e}"}), 500

# ── Action endpoint ───────────────────────────────────────────────────────────

@app.route("/api/action2", methods=["POST"])
def action2():
    """Extended actions: volume attach/detach, FIP, SG rules, rename."""
    body          = request.get_json() or {}
    client_id     = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id    = body.get("projectId", "")
    customer_name = body.get("customerName", "")
    action_type   = body.get("action", "")
    params        = body.get("params", {})

    if customer_name:
        cust = get_customer(customer_name)
        if cust:
            client_id     = cust["client_id"]
            client_secret = cust["client_secret"]
            project_id    = cust["project_id"]
        else:
            return jsonify({"error": f"Customer '{customer_name}' not found"}), 404

    if not client_id or not project_id or not action_type:
        return jsonify({"error": "Cần clientId/customerName, projectId, action"}), 400

    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        ok, data = execute_extended_action(token, uid, project_id, action_type, params)
        return jsonify({"ok": ok, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/action", methods=["POST"])
def action():
    """Execute a confirmed action on GreenNode."""
    body          = request.get_json() or {}
    client_id     = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id    = body.get("projectId", "")
    action_type   = body.get("action", "")
    params        = body.get("params", {})

    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        P   = project_id
        server_id = params.get("serverId", "")

        # Actions that change VM state — we poll for actual status after sending command
        POLL_ACTIONS = {"vm_start", "vm_stop", "vm_reboot"}
        # Expected final state after each action
        EXPECTED_STATE = {"vm_start": "ACTIVE", "vm_stop": "SHUTOFF", "vm_reboot": "ACTIVE"}

        if action_type == "vm_start":
            status, data = gn_api(token, uid, "PUT", f"v2/{P}/servers/{server_id}/start")
        elif action_type == "vm_stop":
            status, data = gn_api(token, uid, "PUT", f"v2/{P}/servers/{server_id}/stop")
        elif action_type == "vm_reboot":
            status, data = gn_api(token, uid, "PUT", f"v2/{P}/servers/{server_id}/reboot", {"type": "SOFT"})
        elif action_type == "sg_attach":
            status, data = gn_api(token, uid, "POST",
                f"v2/{P}/servers/{server_id}/securitygroups",
                {"securityGroupId": params.get("sgId")})
        elif action_type == "sg_detach":
            status, data = gn_api(token, uid, "DELETE",
                f"v2/{P}/servers/{server_id}/securitygroups/{params.get('sgId')}")
        elif action_type == "snapshot_create":
            status, data = gn_api(token, uid, "POST", f"v2/{P}/snapshots", {
                "serverId":    server_id,
                "name":        params.get("name", f"snap-{server_id[:8]}-{datetime.utcnow().strftime('%Y%m%d')}"),
                "description": "Created by GreenNode AI Agent"
            })
        else:
            return jsonify({"error": f"Unknown action: {action_type}"}), 400

        if status >= 300:
            return jsonify({"ok": False, "status": status, "data": data})

        # For VM state-change actions: poll GreenNode until state matches expected or timeout
        if action_type in POLL_ACTIONS:
            import time
            expected = EXPECTED_STATE[action_type]
            actual_state = "UNKNOWN"
            poll_result = {}
            # Poll every 5 seconds, max 60 seconds (12 attempts)
            for attempt in range(12):
                time.sleep(5)
                s2, d2 = gn_api(token, uid, "GET", f"v2/{P}/servers/{server_id}")
                if s2 == 200:
                    # GreenNode returns single server differently — try both response shapes
                    server_data = d2.get("data") or d2.get("server") or d2
                    actual_state = (server_data.get("status") or
                                    server_data.get("serverState") or "UNKNOWN")
                    poll_result = server_data
                    if actual_state == expected:
                        break
                elif s2 == 404:
                    # Try listing servers to find this one
                    s3, d3 = gn_api(token, uid, "GET", f"v2/{P}/servers")
                    if s3 == 200:
                        servers = d3.get("listData", [])
                        match = next((sv for sv in servers if sv.get("uuid") == server_id), None)
                        if match:
                            actual_state = match.get("status", "UNKNOWN")
                            poll_result = match
                            if actual_state == expected:
                                break

            return jsonify({
                "ok":           True,
                "status":       status,
                "data":         data,
                "actualState":  actual_state,
                "expectedState": expected,
                "confirmed":    actual_state == expected,
                "pollResult":   poll_result,
            })

        return jsonify({"ok": status < 300, "status": status, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Microsoft Teams Bot (Azure Bot Framework) ─────────────────────────────────
AZURE_APP_ID       = os.getenv("AZURE_APP_ID", "")
AZURE_APP_PASSWORD = os.getenv("AZURE_APP_PASSWORD", "")

def get_azure_token():
    """Get Azure Bot Framework token to send proactive messages."""
    r = requests.post(
        "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     AZURE_APP_ID,
            "client_secret": AZURE_APP_PASSWORD,
            "scope":         "https://api.botframework.com/.default",
        },
        timeout=10
    )
    return r.json().get("access_token", "")

def send_teams_reply(activity: dict, reply_text: str):
    """Send reply back to Teams via Bot Framework."""
    az_token   = get_azure_token()
    service_url = activity.get("serviceUrl", "")
    conv_id     = activity.get("conversation", {}).get("id", "")
    activity_id = activity.get("id", "")

    reply = {
        "type":         "message",
        "text":         reply_text,
        "conversation": activity.get("conversation"),
        "from":         activity.get("recipient"),
        "recipient":    activity.get("from"),
        "replyToId":    activity_id,
    }
    requests.post(
        f"{service_url}v3/conversations/{conv_id}/activities/{activity_id}",
        headers={"Authorization": f"Bearer {az_token}", "Content-Type": "application/json"},
        json=reply,
        timeout=15
    )

@app.route("/api/teams-bot", methods=["POST"])
def teams_bot():
    """
    Azure Bot Framework endpoint for Teams.
    Teams → Azure Bot → POST here → reply via Bot Framework API.
    """
    activity = request.get_json() or {}
    activity_type = activity.get("type", "")

    # Only handle messages
    if activity_type != "message":
        return jsonify({}), 200

    # Extract message — strip <at>BotName</at> mention
    import re
    raw_text = activity.get("text", "")
    message  = re.sub(r"<at>[^<]*</at>", "", raw_text).strip()
    sender   = activity.get("from", {}).get("name", "User")

    if not message:
        return jsonify({}), 200

    # Use Teams GN credentials from .env
    cid  = TEAMS_GN_CLIENT_ID
    csec = TEAMS_GN_CLIENT_SECRET
    proj = TEAMS_GN_PROJECT_ID

    if not cid or not csec or not proj:
        send_teams_reply(activity,
            "⚠️ Chưa cấu hình GreenNode credentials.\n"
            "Admin vui lòng thêm TEAMS_GN_CLIENT_ID, TEAMS_GN_CLIENT_SECRET, "
            "TEAMS_GN_PROJECT_ID vào file .env và restart server."
        )
        return jsonify({}), 200

    try:
        token, user_info = fetch_gn_token(cid, csec)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        P   = proj

        # Fetch real-time data
        vms, volumes, networks, sgs = [], [], [], []
        s1, d1 = gn_api(token, uid, "GET", f"v2/{P}/servers")
        if s1 == 200: vms = d1.get("listData", [])
        s2, d2 = gn_api(token, uid, "GET", f"v2/{P}/volumes")
        if s2 == 200: volumes = d2.get("listData", [])
        s3, d3 = gn_api(token, uid, "GET", f"v2/{P}/networks")
        if s3 == 200: networks = d3.get("listData", [])

        sg_map = {}
        for s in vms:
            for sg in s.get("secGroups", []):
                k = sg.get("uuid", "")
                if k not in sg_map:
                    sg_map[k] = {**sg, "servers": []}
                sg_map[k]["servers"].append(s["name"])
        sgs = list(sg_map.values())

        fips = []
        for s in vms:
            for iface in s.get("internalInterfaces", []):
                if iface.get("floatingIp"):
                    fips.append({"ip": iface["floatingIp"], "server": s["name"]})

        # Check action intent
        action_type, params, desc = detect_action_intent(message, vms, sgs, volumes)
        if action_type and params:
            lower = message.lower()
            if any(w in lower for w in ["xác nhận", "confirm", "yes", "có", "đồng ý"]):
                ok, err, vm_after = execute_vm_action(token, uid, proj, action_type, params)
                if ok:
                    st = vm_after.get("status", "đang xử lý") if vm_after else "đang xử lý"
                    reply = f"✅ Thành công! VM **{params.get('serverName')}**: {st}"
                else:
                    reply = f"❌ Thất bại: {err}"
            else:
                reply = (f"⚠️ **Xác nhận hành động**\n\n{desc}\n\n"
                         f"Gõ **xác nhận** để thực hiện hoặc **hủy** để bỏ qua.")
            send_teams_reply(activity, reply)
            return jsonify({}), 200

        # Ask LLM
        def fmt_vm(s):
            ip  = s.get("internalInterfaces",[{}])[0].get("fixedIp","N/A") if s.get("internalInterfaces") else "N/A"
            wan = s.get("internalInterfaces",[{}])[0].get("floatingIp","N/A") if s.get("internalInterfaces") else "N/A"
            return f"VM|{s.get('name')}|{s.get('status')}|{ip}|{wan}|{s.get('flavor',{}).get('name','?')}"

        context = f"""PROJECT: {proj} | User: {sender}
VMs ({len(vms)}): {chr(10).join(fmt_vm(s) for s in vms) or "(none)"}
Volumes ({len(volumes)}): {", ".join((v.get("name","")+" "+str(v.get("status",""))) for v in volumes) or "(none)"}
SGs ({len(sgs)}): {", ".join(sg.get("name","") for sg in sgs) or "(none)"}
Networks ({len(networks)}): {", ".join(n.get("name","") for n in networks) or "(none)"}
FIPs ({len(fips)}): {", ".join(f["ip"]+"("+f["server"]+")" for f in fips) or "(none)"}"""

        system_prompt = f"""Bạn là GreenNode AI Assistant trong Microsoft Teams.
Trả lời NGẮN GỌN tối đa 5-6 dòng. Dùng emoji. Không dùng bảng phức tạp.
Dữ liệu real-time từ GreenNode API:
{context}"""

        r = requests.post(
            GN_MAAS_URL,
            headers={"Authorization": f"Bearer {GN_MAAS_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GN_MAAS_MODEL,
                "messages": [
                    {"role": "assistant", "content": system_prompt},
                    {"role": "user",      "content": message}
                ],
                "max_tokens": 500, "temperature": 0.5,
            },
            timeout=30, verify=False,
        )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
        send_teams_reply(activity, reply)

    except Exception as e:
        send_teams_reply(activity, f"❌ Lỗi: {str(e)}")

    return jsonify({}), 200



# ── Scheduled job runner ──────────────────────────────────────────────────────
def run_scheduled_job(job_id: str):
    """Execute a scheduled VM action."""
    job = _scheduled_jobs.get(job_id)
    if not job:
        return
    try:
        creds       = job["creds"]
        action_type = job["action"]
        params      = job["params"]
        token, user_info = fetch_gn_token(creds["clientId"], creds["clientSecret"])
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        ok, err, vm_after = execute_vm_action(token, uid, creds["projectId"], action_type, params)
        status = vm_after.get("status", "?") if vm_after else "unknown"
        print(f"[SCHEDULE] Job {job_id}: {action_type} on {params.get('serverName')} → {status}")
    except Exception as e:
        print(f"[SCHEDULE] Job {job_id} error: {e}")
    finally:
        _scheduled_jobs.pop(job_id, None)


def _do_schedule(client_id, client_secret, project_id, action, params, run_at_str, tz_str="Asia/Ho_Chi_Minh"):
    """Internal schedule logic — callable without HTTP."""
    try:
        tz       = pytz.timezone(tz_str)
        run_time = datetime.fromisoformat(run_at_str)
        if run_time.tzinfo is None:
            run_time = tz.localize(run_time)
        now_tz = datetime.now(tz)
        if run_time <= now_tz:
            diff  = now_tz - run_time
            hours = int(diff.total_seconds() // 3600)
            mins  = int((diff.total_seconds() % 3600) // 60)
            return {"ok": False, "error": f"Thời gian {run_time.strftime('%H:%M ngày %d/%m/%Y')} đã qua {hours}h{mins:02d}p rồi. Vui lòng chọn thời gian trong tương lai."}

        job_id = f"{action}_{params.get('serverId','')[:8]}_{run_time.strftime('%Y%m%d%H%M')}"
        _scheduled_jobs[job_id] = {
            "desc":     f"{action} {params.get('serverName','')} lúc {run_time.strftime('%H:%M %d/%m/%Y')}",
            "action":   action,
            "params":   params,
            "creds":    {"clientId": client_id, "clientSecret": client_secret, "projectId": project_id},
            "run_time": run_time.isoformat(),
        }
        scheduler.add_job(
            run_scheduled_job, trigger="date", run_date=run_time,
            args=[job_id], id=job_id, replace_existing=True,
        )
        action_label = "khởi động" if action == "vm_start" else "tắt"
        server_name  = params.get("serverName", "VM")
        return {
            "ok":      True,
            "message": f"✅ Đã hẹn {action_label} VM **{server_name}** lúc {run_time.strftime('%H:%M ngày %d/%m/%Y')}",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.route("/api/schedule", methods=["POST"])
def schedule_action():
    """Schedule a VM action at a specific time."""
    body          = request.get_json() or {}
    client_id     = body.get("clientId", "")
    client_secret = body.get("clientSecret", "")
    project_id    = body.get("projectId", "")
    action_type   = body.get("action", "")   # vm_start, vm_stop
    params        = body.get("params", {})   # {serverId, serverName}
    run_at_str    = body.get("runAt", "")    # ISO format: "2026-05-20T03:30:00"
    tz_str        = body.get("timezone", "Asia/Ho_Chi_Minh")

    if not all([client_id, project_id, action_type, params, run_at_str]):
        return jsonify({"error": "Thiếu thông tin: clientId, projectId, action, params, runAt"}), 400

    try:
        tz       = pytz.timezone(tz_str)
        run_time = datetime.fromisoformat(run_at_str)
        if run_time.tzinfo is None:
            run_time = tz.localize(run_time)

        now = datetime.now(tz)
        if run_time <= now:
            diff = now - run_time
            hours = int(diff.total_seconds() // 3600)
            mins  = int((diff.total_seconds() % 3600) // 60)
            return jsonify({
                "error": f"⏰ Thời gian {run_time.strftime('%H:%M ngày %d/%m/%Y')} đã qua {hours}h{mins:02d}p rồi. Vui lòng chọn thời gian trong tương lai."
            }), 400

        job_id = f"{action_type}_{params.get('serverId','')[:8]}_{run_time.strftime('%Y%m%d%H%M')}"
        _scheduled_jobs[job_id] = {
            "desc":    f"{action_type} {params.get('serverName','')} lúc {run_time.strftime('%H:%M %d/%m/%Y')}",
            "action":  action_type,
            "params":  params,
            "creds":   {"clientId": client_id, "clientSecret": client_secret, "projectId": project_id},
            "run_time": run_time.isoformat(),
        }

        scheduler.add_job(
            run_scheduled_job,
            trigger="date",
            run_date=run_time,
            args=[job_id],
            id=job_id,
            replace_existing=True,
        )

        action_label = "khởi động" if action_type == "vm_start" else "tắt"
        return jsonify({
            "ok":      True,
            "jobId":   job_id,
            "message": f"✅ Đã hẹn {action_label} VM **{params.get('serverName')}** lúc {run_time.strftime('%H:%M ngày %d/%m/%Y')}",
            "runAt":   run_time.isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/schedule", methods=["GET"])
def list_schedules():
    """List all pending scheduled jobs."""
    jobs = []
    for job_id, job in _scheduled_jobs.items():
        jobs.append({
            "jobId":   job_id,
            "desc":    job["desc"],
            "action":  job["action"],
            "server":  job["params"].get("serverName", ""),
            "runAt":   job["run_time"],
        })
    return jsonify({"jobs": jobs, "count": len(jobs)})

@app.route("/api/schedule/<job_id>", methods=["DELETE"])
def cancel_schedule(job_id):
    """Cancel a scheduled job."""
    if job_id in _scheduled_jobs:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        job = _scheduled_jobs.pop(job_id)
        return jsonify({"ok": True, "message": f"Đã hủy lịch: {job['desc']}"})
    return jsonify({"error": "Không tìm thấy job"}), 404


# ── Extended actions (Volume, FIP, SG rules, Tag) ────────────────────────────
def execute_extended_action(token, uid, project_id, action_type, params):
    """Execute non-VM actions: volume, FIP, SG rules, rename."""
    P = project_id
    ok_statuses = (200, 201, 202, 204)

    # ── Volume attach/detach ─────────────────────────────────────────────────
    if action_type == "volume_attach":
        server_id = params.get("serverId")
        volume_id = params.get("volumeId")
        print(f"[ATTACH] server={server_id} volume={volume_id} uid={uid} project={P}")
        
        # Correct endpoint from GreenNode docs: PUT /v2/{project}/volumes/{vol}/servers/{server}/attach
        body_attach = {"persistentVolume": True, "tags": [], "zoneId": ""}
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/volumes/{volume_id}/servers/{server_id}/attach",
            body_attach)
        print(f"[ATTACH] PUT volumes/.../servers/.../attach -> {s} {str(d)[:200]}")
        return s in ok_statuses, d

    if action_type == "volume_detach":
        server_id = params.get("serverId")
        volume_id = params.get("volumeId")
        status, data = gn_api(token, uid, "DELETE",
            f"v2/{P}/servers/{server_id}/detachvolume/{volume_id}")
        return status in ok_statuses, data

    # ── Floating IP associate/disassociate ───────────────────────────────────
    if action_type == "fip_associate":
        server_id   = params.get("serverId")
        floating_ip = params.get("floatingIp")
        status, data = gn_api(token, uid, "POST",
            f"v2/{P}/servers/{server_id}/addfloatingip",
            {"floatingIp": floating_ip})
        return status in ok_statuses, data

    if action_type == "fip_disassociate":
        server_id   = params.get("serverId")
        floating_ip = params.get("floatingIp")
        status, data = gn_api(token, uid, "POST",
            f"v2/{P}/servers/{server_id}/removefloatingip",
            {"floatingIp": floating_ip})
        return status in ok_statuses, data

    # ── SG rule add/remove ───────────────────────────────────────────────────
    if action_type == "sg_rule_add":
        sg_id = params.get("sgId")
        rule  = params.get("rule", {})
        # rule = {protocol, direction, portRangeMin, portRangeMax, remoteIpPrefix, ethertype}
        status, data = gn_api(token, uid, "POST",
            f"v2/{P}/securitygroups/{sg_id}/securitygrouprules", rule)
        return status in ok_statuses, data

    if action_type == "sg_rule_remove":
        sg_id   = params.get("sgId")
        rule_id = params.get("ruleId")
        status, data = gn_api(token, uid, "DELETE",
            f"v2/{P}/securitygroups/{sg_id}/securitygrouprules/{rule_id}")
        return status in ok_statuses, data

    # ── Rename VM ────────────────────────────────────────────────────────────
    if action_type == "vm_rename":
        server_id = params.get("serverId")
        new_name  = params.get("newName")
        status, data = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}",
            {"name": new_name})
        return status in ok_statuses, data

    # ── Rename Volume ────────────────────────────────────────────────────────
    if action_type == "volume_rename":
        volume_id = params.get("volumeId")
        new_name  = params.get("newName")
        status, data = gn_api(token, uid, "PUT",
            f"v2/{P}/volumes/{volume_id}",
            {"name": new_name})
        return status in ok_statuses, data

    return False, {"error": f"Unknown action: {action_type}"}

# ── Teams Tab config page ─────────────────────────────────────────────────────
@app.route("/teams-config")
def teams_config():
    """Required by Teams for configurable tabs."""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<script src="https://res.cdn.office.net/teams-js/2.22.0/js/MicrosoftTeams.min.js"></script>
<style>
  body{font-family:-apple-system,sans-serif;padding:2rem;background:#f0f2f5;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .box{background:#fff;padding:2rem;border-radius:12px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,0.1)}
  h2{color:#185fa5;margin-bottom:1rem}
  p{color:#555;margin-bottom:1.5rem}
  button{background:#185fa5;color:#fff;border:none;padding:10px 24px;border-radius:8px;font-size:14px;cursor:pointer}
</style>
</head>
<body>
<div class="box">
  <h2>🖥️ GreenNode AI Agent</h2>
  <p>Click Save để thêm GreenNode chatbot vào channel này.</p>
  <button onclick="save()">Save</button>
</div>
<script>
microsoftTeams.app.initialize().then(() => {
  microsoftTeams.pages.config.registerOnSaveHandler((saveEvent) => {
    microsoftTeams.pages.config.setConfig({
      suggestedDisplayName: "GreenNode",
      entityId: "greennode-tab",
      contentUrl: window.location.origin + "/",
      websiteUrl: window.location.origin + "/"
    });
    saveEvent.notifySuccess();
  });
});
function save() {
  microsoftTeams.pages.config.setValidityState(true);
}
</script>
</body>
</html>"""


# ── End-user customer chat page ───────────────────────────────────────────────
@app.route("/customer")
def customer_page():
    return send_from_directory("static", "customer.html")


# admin_required moved to top of file

@app.route("/login", methods=["GET"])
def login_page():
    return """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>GreenNode Admin — Đăng nhập</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#fff;border-radius:14px;padding:36px 32px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
.logo{width:44px;height:44px;background:#185fa5;border-radius:10px;display:flex;align-items:center;justify-content:center;margin:0 auto 16px}
.logo svg{stroke:#fff}
h2{text-align:center;font-size:18px;font-weight:500;color:#1a1a1a;margin-bottom:4px}
p{text-align:center;font-size:13px;color:#888;margin-bottom:24px}
label{font-size:13px;color:#555;display:block;margin-bottom:5px}
input{width:100%;padding:9px 12px;border-radius:8px;border:1px solid #ddd;font-size:14px;margin-bottom:14px;font-family:inherit}
input:focus{outline:none;border-color:#378add}
button{width:100%;padding:10px;background:#185fa5;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
button:hover{background:#0c447c}
.err{color:#e53935;font-size:13px;text-align:center;margin-bottom:12px;display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
  </div>
  <h2>GreenNode Admin</h2>
  <p>Đăng nhập để quản lý khách hàng</p>
  <div class="err" id="err">Sai username hoặc password</div>
  <form id="form">
    <label>Username</label>
    <input id="u" type="text" placeholder="admin" autocomplete="username"/>
    <label>Password</label>
    <input id="p" type="password" placeholder="••••••••" autocomplete="current-password"/>
    <button type="submit">Đăng nhập</button>
  </form>
</div>
<script>
document.getElementById('form').addEventListener('submit', async e => {
  e.preventDefault();
  const r = await fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({username: document.getElementById('u').value, password: document.getElementById('p').value})
  });
  const d = await r.json();
  if (d.ok) {
    localStorage.setItem('gn_admin_token', d.token);
    window.location.href = '/';
  } else { document.getElementById('err').style.display = 'block'; }
});
document.getElementById('u').focus();
</script>
</body>
</html>"""

import hashlib, secrets as _secrets

def make_admin_token():
    """Generate a deterministic token from credentials."""
    raw = f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}:{app.secret_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

@app.route("/api/login", methods=["POST"])
def api_login():
    body = request.get_json() or {}
    if body.get("username") == ADMIN_USERNAME and body.get("password") == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        token = make_admin_token()
        return jsonify({"ok": True, "token": token})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/verify-token", methods=["POST"])
def verify_token():
    body = request.get_json() or {}
    token = body.get("token", "")
    valid = token == make_admin_token()
    if valid:
        session["admin_logged_in"] = True
    return jsonify({"ok": valid})

# ── Serve static chatbot UI ───────────────────────────────────────────────────
@app.route("/")
@admin_required
def index():
    return send_from_directory("static", "index.html")


# ── Microsoft Teams Outgoing Webhook ─────────────────────────────────────────
import hmac, hashlib, base64

TEAMS_WEBHOOK_SECRET = os.getenv("TEAMS_WEBHOOK_SECRET", "")
# Default GreenNode credentials for Teams (set in .env)
TEAMS_GN_CLIENT_ID     = os.getenv("TEAMS_GN_CLIENT_ID", "")
TEAMS_GN_CLIENT_SECRET = os.getenv("TEAMS_GN_CLIENT_SECRET", "")
TEAMS_GN_PROJECT_ID    = os.getenv("TEAMS_GN_PROJECT_ID", "")

def verify_teams_signature(request_body: bytes, auth_header: str) -> bool:
    """Verify Teams HMAC signature to ensure request is from Microsoft."""
    if not TEAMS_WEBHOOK_SECRET or not auth_header:
        return True  # Skip verification if secret not configured
    try:
        token = auth_header.replace("HMAC ", "")
        secret_bytes = base64.b64decode(TEAMS_WEBHOOK_SECRET)
        expected = base64.b64encode(
            hmac.new(secret_bytes, request_body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(token, expected)
    except Exception:
        return False

def teams_card(text: str) -> dict:
    """Build a simple Teams Adaptive Card response."""
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type":    "AdaptiveCard",
                "version": "1.4",
                "body": [{
                    "type": "TextBlock",
                    "text": text,
                    "wrap": True,
                    "markdown": True,
                }]
            }
        }]
    }

def teams_simple(text: str) -> dict:
    """Simple text response for Teams."""
    return {"type": "message", "text": text}

@app.route("/api/teams", methods=["POST"])
def teams_webhook():
    """
    Microsoft Teams Outgoing Webhook endpoint.
    Teams sends POST here when user types @BotName <message>.
    Setup: Teams → channel settings → Manage → Outgoing Webhooks → Add
    """
    # Verify signature
    raw_body = request.get_data()
    auth_header = request.headers.get("Authorization", "")
    if not verify_teams_signature(raw_body, auth_header):
        return jsonify({"type": "message", "text": "❌ Unauthorized"}), 401

    body = request.get_json() or {}
    # Extract message text — strip bot mention (@BotName)
    raw_text = body.get("text", "")
    # Teams wraps mention in <at>BotName</at> — strip it
    import re
    message = re.sub(r"<at>[^<]*</at>", "", raw_text).strip()
    sender  = body.get("from", {}).get("name", "User")

    if not message:
        return jsonify(teams_simple("Xin chào! Hỏi tôi về hạ tầng GreenNode của bạn."))

    # Use Teams-specific credentials from .env
    cid   = TEAMS_GN_CLIENT_ID
    csec  = TEAMS_GN_CLIENT_SECRET
    proj  = TEAMS_GN_PROJECT_ID

    if not cid or not csec or not proj:
        return jsonify(teams_simple(
            "⚠️ Chưa cấu hình GreenNode credentials cho Teams.\n"
            "Thêm TEAMS_GN_CLIENT_ID, TEAMS_GN_CLIENT_SECRET, TEAMS_GN_PROJECT_ID vào .env"
        ))

    try:
        # Fetch GreenNode token + data
        token, user_info = fetch_gn_token(cid, csec)
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        P   = proj

        vms, volumes, networks = [], [], []
        s1, d1 = gn_api(token, uid, "GET", f"v2/{P}/servers")
        if s1 == 200: vms = d1.get("listData", [])
        s2, d2 = gn_api(token, uid, "GET", f"v2/{P}/volumes")
        if s2 == 200: volumes = d2.get("listData", [])
        s3, d3 = gn_api(token, uid, "GET", f"v2/{P}/networks")
        if s3 == 200: networks = d3.get("listData", [])

        # SG from VMs
        sg_map = {}
        for s in vms:
            for sg in s.get("secGroups", []):
                k = sg.get("uuid", "")
                if k not in sg_map:
                    sg_map[k] = {**sg, "servers": []}
                sg_map[k]["servers"].append(s["name"])
        sgs = list(sg_map.values())

        # FIPs
        fips = []
        for s in vms:
            for iface in s.get("internalInterfaces", []):
                if iface.get("floatingIp"):
                    fips.append({"ip": iface["floatingIp"], "server": s["name"], "status": iface.get("status","")})

        # Check for action intent (stop/start/reboot)
        action_type, params, desc = detect_action_intent(message, vms, sgs, volumes)
        if action_type and params:
            # For Teams: execute action directly (no confirm flow)
            # Add confirmation word detection
            confirm_words = ["xác nhận", "confirm", "yes", "có", "đồng ý"]
            cancel_words  = ["hủy", "cancel", "không", "no"]
            lower_msg = message.lower()

            if any(w in lower_msg for w in confirm_words):
                ok, err, vm_after = execute_vm_action(token, uid, proj, action_type, params)
                if ok:
                    st = vm_after.get("status","?") if vm_after else "đang xử lý"
                    return jsonify(teams_simple(f"✅ Thành công! VM {params.get('serverName')}: {st}"))
                else:
                    return jsonify(teams_simple(f"❌ Thất bại: {err}"))
            else:
                # Ask for confirmation in Teams
                return jsonify(teams_simple(
                    f"⚠️ **Xác nhận hành động**\n\n{desc}\n\n"
                    f"Gõ **@Bot xác nhận** để thực hiện hoặc **@Bot hủy** để bỏ qua."
                ))

        # Build context and ask LLM
        def fmt_vm(s):
            ip  = s.get("internalInterfaces",[{}])[0].get("fixedIp","N/A") if s.get("internalInterfaces") else "N/A"
            wan = s.get("internalInterfaces",[{}])[0].get("floatingIp","N/A") if s.get("internalInterfaces") else "N/A"
            sgs_str = ", ".join(g.get("name","") for g in s.get("secGroups",[]))
            return f"VM|{s.get('name')}|{s.get('status')}|{ip}|{wan}|{s.get('flavor',{}).get('name','?')}|SG:[{sgs_str}]"

        context = f"""PROJECT: {proj} | Hỏi bởi: {sender}
VMs ({len(vms)}): {chr(10).join(fmt_vm(s) for s in vms) or "(none)"}
Volumes ({len(volumes)}): {", ".join(v.get("name","") + " " + str(v.get("status","")) for v in volumes) or "(none)"}
Security Groups ({len(sgs)}): {", ".join(sg.get("name","") for sg in sgs) or "(none)"}
Networks ({len(networks)}): {", ".join(n.get("name","") for n in networks) or "(none)"}
Floating IPs ({len(fips)}): {", ".join(f["ip"]+" ("+f["server"]+")" for f in fips) or "(none)"}"""

        system_prompt = f"""Bạn là GreenNode AI Assistant trong Microsoft Teams.
Trả lời NGẮN GỌN (tối đa 5 dòng) vì Teams có giới hạn hiển thị.
Dùng emoji thay cho markdown phức tạp. Không dùng bảng.
Dữ liệu real-time:
{context}"""

        r = requests.post(
            GN_MAAS_URL,
            headers={"Authorization": f"Bearer {GN_MAAS_API_KEY}", "Content-Type": "application/json"},
            json={"model": GN_MAAS_MODEL, "messages": [
                {"role": "assistant", "content": system_prompt},
                {"role": "user",      "content": message}
            ], "max_tokens": 500, "temperature": 0.5},
            timeout=30, verify=False,
        )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
        return jsonify(teams_simple(reply))

    except Exception as e:
        return jsonify(teams_simple(f"❌ Lỗi: {str(e)}"))

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
