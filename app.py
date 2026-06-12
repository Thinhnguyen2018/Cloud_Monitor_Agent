"""
GreenNode AI Agent — Flask Backend
Deploy: gunicorn app:app -w 2 -b 0.0.0.0:8000
"""
import os, re, json, hashlib, base64, requests
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from flask_cors import CORS
from functools import wraps
from datetime import datetime, timedelta
import threading
from dotenv import load_dotenv
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

# ── Static reference data (images / flavors / volume-types per region/zone) ───
_REF_DIR = os.path.join(os.path.dirname(__file__), "references")

def _load_ref(region: str, zone: str, kind: str) -> dict:
    """Load references/{region}/{zone}/{kind}.json — return {} on missing."""
    path = os.path.join(_REF_DIR, region, zone, f"{kind}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def ref_images(region="HCM", zone="HCM03-1A") -> list:
    """Flat list of {name, id, os, version} from static reference."""
    d = _load_ref(region, zone, "images")
    out = []
    for os_family, versions in d.get("images", {}).items():
        for ver_name, meta in versions.items():
            if isinstance(meta, dict) and meta.get("id"):
                out.append({
                    "name": ver_name,
                    "id":   meta["id"],
                    "os":   os_family,
                    "uefi": meta.get("uefi", False),
                    "recommended": meta.get("recommended", False),
                })
    return out

def _estimate_price_vnd(cpu: int, ram_gb: int, family: str = "general", generation: str = "s2") -> int:
    """
    Estimate monthly price in VND based on VNG Cloud public pricing (approximate).
    S2 generation pricing: CPU ~180,000 VND/vCPU/month, RAM ~36,000 VND/GB/month
    S1 (deprecated): ~120,000 VND/vCPU/month, ~24,000 VND/GB/month
    HighMem: +20% RAM premium. HighCPU: standard.
    """
    if "s2" in generation or generation == "":
        cpu_rate = 180_000
        ram_rate = 36_000
    else:  # s1 deprecated
        cpu_rate = 120_000
        ram_rate = 24_000
    if "highmem" in family.lower():
        ram_rate = int(ram_rate * 1.2)
    return cpu * cpu_rate + ram_gb * ram_rate

def ref_flavors(region="HCM", zone="HCM03-1A") -> list:
    """Flat list of {name, id, cpu, ram_gb, preferred, price_vnd} from static reference."""
    d = _load_ref(region, zone, "flavors")
    out = []
    for flav_name, meta in d.get("flavors", {}).items():
        if isinstance(meta, dict) and meta.get("id"):
            cpu    = meta.get("cpu", 0)
            ram_gb = meta.get("ram_gb", 0)
            family = meta.get("family", "")
            gen    = meta.get("generation", "s2")
            out.append({
                "name":       flav_name,
                "id":         meta["id"],
                "cpu":        cpu,
                "ram_gb":     ram_gb,
                "family":     family,
                "network":    meta.get("network", ""),
                "preferred":  meta.get("preferred", False),
                "deprecated": meta.get("deprecated", False),
                "generation": gen,
                "price_vnd":  _estimate_price_vnd(cpu, ram_gb, family, gen),
            })
    return out

def ref_vol_types(region="HCM", zone="HCM03-1A") -> list:
    """Flat list of {name, id, iops, default} from static reference."""
    d = _load_ref(region, zone, "volume-types")
    out = []
    for vt_name, meta in d.get("volume_types", {}).items():
        if isinstance(meta, dict) and meta.get("id"):
            out.append({
                "name":    vt_name,
                "id":      meta["id"],
                "iops":    meta.get("iops", 0),
                "default": meta.get("default", False),
            })
    return out

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
    ph   = "%s" if (USE_PG and DATABASE_URL) else "?"
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
        # Scheduled jobs — persist across restarts
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                job_id      TEXT PRIMARY KEY,
                customer    TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                action      TEXT NOT NULL,
                params      TEXT NOT NULL,
                creds       TEXT NOT NULL,
                run_time    TEXT NOT NULL,
                description TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                result      TEXT DEFAULT '',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Audit log — all actions performed
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          SERIAL PRIMARY KEY,
                customer    TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                action      TEXT NOT NULL,
                resource    TEXT NOT NULL,
                params      TEXT NOT NULL,
                status      TEXT NOT NULL,
                message     TEXT DEFAULT '',
                performed_by TEXT DEFAULT 'admin',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Notifications
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id          SERIAL PRIMARY KEY,
                customer    TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL,
                type        TEXT DEFAULT 'info',
                read        BOOLEAN DEFAULT FALSE,
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                job_id      TEXT PRIMARY KEY,
                customer    TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                action      TEXT NOT NULL,
                params      TEXT NOT NULL,
                creds       TEXT NOT NULL,
                run_time    TEXT NOT NULL,
                description TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                result      TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer    TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                action      TEXT NOT NULL,
                resource    TEXT NOT NULL,
                params      TEXT NOT NULL,
                status      TEXT NOT NULL,
                message     TEXT DEFAULT '',
                performed_by TEXT DEFAULT 'admin',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                customer    TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT NOT NULL,
                type        TEXT DEFAULT 'info',
                read        INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
    conn.commit()
    conn.close()

# ── DB helpers ────────────────────────────────────────────────────────────────
_PH = "%s" if (USE_PG and DATABASE_URL) else "?"

def db_write_schedule(job_id, customer, project_id, action, params, creds, run_time, description):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"""INSERT OR REPLACE INTO scheduled_jobs
        (job_id,customer,project_id,action,params,creds,run_time,description,status)
        VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},'pending')""",
        (job_id, customer, project_id, action, json.dumps(params), json.dumps(creds), run_time, description))
    conn.commit(); conn.close()

def db_update_schedule_status(job_id, status, result=""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE scheduled_jobs SET status={_PH}, result={_PH} WHERE job_id={_PH}", (status, result, job_id))
    conn.commit(); conn.close()

def db_delete_schedule(job_id):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"DELETE FROM scheduled_jobs WHERE job_id={_PH}", (job_id,))
    conn.commit(); conn.close()

def db_get_pending_schedules():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_jobs WHERE status='pending' ORDER BY run_time")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close(); return rows

def db_write_audit(customer, project_id, action, resource, params, status, message, performed_by="admin"):
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(f"""INSERT INTO audit_log
            (customer,project_id,action,resource,params,status,message,performed_by)
            VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH})""",
            (customer, project_id, action, resource, json.dumps(params) if isinstance(params,dict) else str(params),
             status, message, performed_by))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[AUDIT] write error: {e}")

def db_get_audit(customer=None, limit=50):
    conn = get_conn(); cur = conn.cursor()
    if customer:
        cur.execute(f"SELECT * FROM audit_log WHERE customer={_PH} ORDER BY created_at DESC LIMIT {_PH}", (customer, limit))
    else:
        cur.execute(f"SELECT * FROM audit_log ORDER BY created_at DESC LIMIT {_PH}", (limit,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close(); return rows

def db_write_notification(customer, title, body, ntype="info"):
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(f"""INSERT INTO notifications (customer,title,body,type)
            VALUES ({_PH},{_PH},{_PH},{_PH})""", (customer, title, body, ntype))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[NOTIF] write error: {e}")

def db_get_notifications(customer, unread_only=False):
    conn = get_conn(); cur = conn.cursor()
    q = f"SELECT * FROM notifications WHERE customer={_PH}"
    if unread_only: q += " AND read=0"
    q += " ORDER BY created_at DESC LIMIT 50"
    cur.execute(q, (customer,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close(); return rows

def db_mark_notifications_read(customer):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE notifications SET read=1 WHERE customer={_PH}", (customer,))
    conn.commit(); conn.close()

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
_scheduled_jobs = {}  # job_id → {desc, action, params, creds, run_time, customer}

def _restore_scheduled_jobs():
    """On startup: reload pending jobs from DB back into APScheduler."""
    tz = pytz.timezone('Asia/Ho_Chi_Minh')
    now = datetime.now(tz)
    rows = db_get_pending_schedules()
    restored = 0
    for row in rows:
        try:
            run_time = datetime.fromisoformat(row['run_time'])
            if run_time.tzinfo is None:
                run_time = tz.localize(run_time)
            if run_time <= now:
                db_update_schedule_status(row['job_id'], 'expired', 'Missed — server was offline')
                continue
            job_id = row['job_id']
            _scheduled_jobs[job_id] = {
                "desc":     row['description'],
                "action":   row['action'],
                "params":   json.loads(row['params']),
                "creds":    json.loads(row['creds']),
                "run_time": row['run_time'],
                "customer": row['customer'],
            }
            scheduler.add_job(run_scheduled_job, trigger="date", run_date=run_time,
                              args=[job_id], id=job_id, replace_existing=True)
            restored += 1
        except Exception as e:
            print(f"[RESTORE] Failed job {row.get('job_id')}: {e}")
    if restored:
        print(f"[RESTORE] Restored {restored} scheduled jobs from DB")

_restore_scheduled_jobs()

@app.after_request
def add_headers(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
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

    # ── VM creation guide (no params yet — show options) ────────────────────
    CREATE_KEYWORDS = ["tạo vm", "tạo server", "tạo máy chủ", "tạo máy ảo", "new vm", "create vm",
                       "tạo mới vm", "tạo mới server", "tạo instance", "tạo 1 vm", "tạo một vm"]
    if any(w in msg for w in CREATE_KEYWORDS):
        # Extract VM name if mentioned
        name_m = re.search(r'(?:tên|name)[:\s]+([^\s,;]+)', message, re.IGNORECASE)
        vm_name = name_m.group(1) if name_m else None
        return ("vm_create_guide", {"vmName": vm_name or ""}, "Hướng dẫn tạo VM mới")

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
    # Guard: nếu là câu hỏi (có "nào", "đang", "?", "liệt", "list", "xem") → không trigger action
    _is_query = any(w in msg for w in ["nào", "đang", "?", "liệt", "list", "xem", "bao nhiêu", "tất cả", "danh sách", "show", "status", "trạng thái"])

    # "tóm tắt" should NOT trigger vm_stop — check it's not part of "tóm tắt"
    has_stop = (not _is_query) and (
        any(w in msg for w in ["stop", "dừng", "shutdown"]) or
        ("shut" in msg and "shutoff" not in msg) or
        ("tắt" in msg and "tóm tắt" not in msg and "tóm" not in msg)
    )
    if has_stop:
        if any(w in msg for w in ["vm", "server", "máy"]) or find_vm(msg):
            vm = find_vm(msg)
            if vm:
                return ("vm_stop", {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                        f"Dừng VM **{vm.get('name')}** (ACTIVE → SHUTOFF)")
            return ("vm_stop", None, "Bạn muốn dừng VM nào?")

    has_start = (not _is_query) and (
        any(w in msg for w in ["start", "khởi động", "turn on"]) or
        (any(w in msg for w in ["bật", "mở"]) and not any(w in msg for w in SCHEDULE_KEYWORDS))
    )
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
        m = re.search(r'volume\s+([\w\-\.]+)', text.lower())
        if m:
            keyword = m.group(1)
            for vol in volumes:
                vname = (vol.get("name") or vol.get("volumeName") or "").lower()
                if keyword in vname or vname in keyword:
                    return vol
        return None

    if any(w in msg for w in ["gắn volume", "attach volume", "gắn disk", "muốn gắn", "gắn vào"]):
        vm = find_vm(msg)
        vol = find_volume(msg)
        if vm and vol:
            vol_id   = vol.get("uuid") or vol.get("id") or vol.get("volumeId")
            vol_name = vol.get("name") or vol.get("volumeName")
            # zoneId: use volumeTypeZoneName (e.g. "HCM03-1B") or zone uuid from volume
            vol_type  = vol.get("volumeType") or {}
            _zone_obj = vol.get("zone") or {}
            zone_id   = (vol.get("volumeTypeZoneName") or
                         (vol_type.get("zoneId")) or
                         (_zone_obj.get("name") if isinstance(_zone_obj, dict) else "") or
                         vol.get("zoneId") or "")
            _vm_id = vm.get("uuid") or vm.get("id") or ""
            if _vm_id and not _vm_id.startswith("ins-"):
                _vm_id = "ins-" + _vm_id
            _vol_id = vol_id or ""
            if _vol_id and not _vol_id.startswith("vol-"):
                _vol_id = "vol-" + _vol_id
            return ("volume_attach",
                    {"serverId": _vm_id, "serverName": vm.get("name"),
                     "volumeId": _vol_id, "volumeName": vol_name, "zoneId": zone_id},
                    f"Gắn volume **{vol_name}** (ID: `{str(vol_id)[:8]}...`) vào VM **{vm.get('name')}**")
        missing = f"tên VM{' ✓' if vm else ' ✗'} và tên Volume{' ✓' if vol else ' ✗'}"
        return ("volume_attach", None, f"Không tìm thấy: {missing}. Hỏi 'liệt kê volume' để xem danh sách.")

    if any(w in msg for w in ["gỡ volume", "detach volume", "tháo disk", "gỡ disk", "muốn gỡ", "gỡ khỏi"]):
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
    if any(w in msg for w in ["gắn floating", "associate ip", "gắn ip công cộng", "gắn wan", "gắn ip"]):
        vm = find_vm(msg)
        ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', msg)
        fip_addr = ip_match.group(1) if ip_match else None
        # Find wanIpId from networks/floating IPs list
        wan_ip_id = fip_addr  # fallback to IP address if no ID found
        if vm:
            return ("fip_associate",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"),
                     "wanIpId": wan_ip_id, "floatingIp": fip_addr},
                    f"Gắn Floating IP **{fip_addr or '?'}** vào VM **{vm.get('name')}**")
        return ("fip_associate", None, "Cần biết tên VM và địa chỉ Floating IP cần gắn")
    if any(w in msg for w in ["gỡ floating", "disassociate ip", "gỡ ip công cộng", "gỡ wan", "gỡ ip"]):
        vm = find_vm(msg)
        if vm:
            # Get current WAN IP from VM info
            wan_ips = vm.get("externalInterfaces", []) or vm.get("wanIps", [])
            wan_ip_id = wan_ips[0].get("uuid") if wan_ips else None
            return ("fip_disassociate",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "wanIpId": wan_ip_id},
                    f"Gỡ Floating IP khỏi VM **{vm.get('name')}**")
        return ("fip_disassociate", None, "Bạn muốn gỡ Floating IP khỏi VM nào?")

    # ── Rename ────────────────────────────────────────────────────────────────
    if any(w in msg for w in ["đổi tên", "rename", "doi ten"]):
        m = re.search(r'(?:thanh|thành|sang|to)\s+([\w\-\.]+)', message, re.IGNORECASE)
        new_name = m.group(1) if m else None
        if not new_name:
            words = [w for w in message.split() if len(w) > 3]
            new_name = words[-1] if words else None
        vm = find_vm(msg)
        if vm and new_name and new_name.lower() != vm.get("name","").lower():
            return ("vm_rename",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "newName": new_name},
                    f"Đổi tên VM **{vm.get('name')}** thành **{new_name}**")
        return ("vm_rename", None, "Bạn muốn đổi tên VM nào thành gì?")
    # ── Security Group Rules ─────────────────────────────────────────────────
    if any(w in msg for w in ["thêm rule", "add rule", "mở port", "open port", "thêm inbound", "thêm outbound"]):
        sg = None
        for s in sgs:
            sname = (s.get("name") or "").lower()
            if sname and sname in msg:
                sg = s
                break
        # Extract port
        port_m = re.search(r'port\s+(\d+)|(\d+)\s*/\s*tcp|(\d+)\s*/\s*udp', msg)
        port = int(port_m.group(1) or port_m.group(2) or port_m.group(3)) if port_m else None
        # Direction
        direction = "egress" if any(w in msg for w in ["outbound", "egress", "ra"]) else "ingress"
        # Protocol
        protocol = "udp" if "udp" in msg else "tcp"
        if sg and port:
            rule = {
                "direction": direction,
                "etherType": "IPv4",
                "portRangeMin": port,
                "portRangeMax": port,
                "protocol": protocol,
                "remoteIpPrefix": "0.0.0.0/0",
                "description": f"Allow {protocol} {port} {direction}"
            }
            return ("sg_rule_add",
                    {"sgId": sg.get("uuid"), "sgName": sg.get("name"), "rule": rule},
                    f"Thêm rule **{direction} {protocol} port {port}** vào Security Group **{sg.get('name')}**")
        return ("sg_rule_add", None, "Cần biết: tên Security Group và port cần mở")

    if any(w in msg for w in ["xóa rule", "remove rule", "xoá rule", "delete rule", "đóng port", "close port"]):
        sg = find_sg(msg)
        rule_m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', msg)
        rule_id = rule_m.group(1) if rule_m else None
        if sg and rule_id:
            return ("sg_rule_remove",
                    {"sgId": sg.get("uuid"), "sgName": sg.get("name"), "ruleId": rule_id},
                    f"Xóa rule `{rule_id[:8]}…` khỏi SG **{sg.get('name')}**")
        hint = (f"Gõ **xem rule {sg.get('name')}** để lấy Rule ID trước." if sg
                else "Gõ **xem rule [tên SG]** để xem danh sách rules và lấy ID.")
        return ("sg_rule_remove", None, hint)

    # ── Delete VM ─────────────────────────────────────────────────────────────
    if any(w in msg for w in ["xóa vm", "xoá vm", "delete vm", "xóa server", "xoá server"]):
        vm = find_vm(msg)
        if vm:
            return ("vm_delete",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                    f"⚠️ XÓA VĨNH VIỄN VM **{vm.get('name')}** — không thể khôi phục!")
        return ("vm_delete", None, "Bạn muốn xóa VM nào?")

    # ── Resize VM ─────────────────────────────────────────────────────────────
    if any(w in msg for w in ["resize vm", "nâng cấp vm", "đổi flavor", "thay đổi cấu hình"]):
        vm = find_vm(msg)
        flavor_m = re.search(r'(flav-[\w\-]+)', msg)
        flavor_id = flavor_m.group(1) if flavor_m else None
        if vm and flavor_id:
            return ("vm_resize",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "flavorId": flavor_id},
                    f"Resize VM **{vm.get('name')}** sang flavor **{flavor_id}**")
        hint = f"VM **{vm.get('name')}** hiện dùng flavor `{vm.get('flavor',{}).get('name','?')}`. " if vm else ""
        return ("vm_resize", None, f"{hint}Hỏi **liệt kê flavor** để xem danh sách, sau đó gõ: **resize vm [tên] sang [flavor_id]**")

    # ── Delete Volume ─────────────────────────────────────────────────────────
    if any(w in msg for w in ["xóa volume", "xoá volume", "delete volume"]):
        vol = find_volume(msg)
        if vol:
            vol_name = vol.get("name") or vol.get("volumeName")
            if "boot" in (vol_name or "").lower():
                return (None, None, "Không thể xóa boot volume!")
            return ("volume_delete",
                    {"volumeId": vol.get("uuid"), "volumeName": vol_name},
                    f"⚠️ XÓA VĨNH VIỄN Volume **{vol_name}** — không thể khôi phục!")
        return ("volume_delete", None, "Bạn muốn xóa Volume nào?")

    # ── Snapshot ─────────────────────────────────────────────────────────────
    if any(w in msg for w in ["snapshot", "tạo snapshot", "chụp", "backup vm"]):
        vm = find_vm(msg)
        if vm:
            m = re.search(r'(?:tên|name)\s+([\w\-\.]+)', message, re.IGNORECASE)
            snap_name = m.group(1) if m else f"snapshot-{vm.get('name','vm')}"
            return ("vm_snapshot",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "snapshotName": snap_name},
                    f"Tạo snapshot VM **{vm.get('name')}** với tên **{snap_name}**")
        return ("vm_snapshot", None, "Bạn muốn tạo snapshot cho VM nào?")

    # ── Rename Volume ─────────────────────────────────────────────────────────
    if any(w in msg for w in ["đổi tên volume", "rename volume", "đổi tên vol"]):
        vol = find_volume(msg)
        m = re.search(r'(?:thanh|thành|sang|to)\s+([\w\-\.]+)', message, re.IGNORECASE)
        new_name = m.group(1) if m else None
        if vol and new_name:
            vol_name = vol.get("name") or vol.get("volumeName")
            return ("volume_rename",
                    {"volumeId": vol.get("uuid"), "volumeName": vol_name, "newName": new_name},
                    f"Đổi tên Volume **{vol_name}** thành **{new_name}**")
        return ("volume_rename", None, "Cần biết tên Volume hiện tại và tên mới. VD: 'đổi tên volume data-vol thành backup-vol'")

    # ── List SG rules ─────────────────────────────────────────────────────────
    if any(w in msg for w in ["xem rule", "liệt kê rule", "danh sách rule", "show rule", "list rule", "rules của", "rule sg"]):
        sg = find_sg(msg)
        if sg:
            return ("sg_list_rules", {"sgId": sg.get("uuid"), "sgName": sg.get("name")},
                    f"Danh sách rules của SG **{sg.get('name')}**")
        return ("sg_list_rules", None, "Bạn muốn xem rules của Security Group nào?")

    # ── Audit log ─────────────────────────────────────────────────────────────
    if any(w in msg for w in ["audit", "lịch sử thao tác", "activity log", "event log", "hoạt động của vm", "sự kiện vm", "lịch sử vm"]):
        vm = find_vm(msg)
        if vm:
            return ("resource_audit", {"serverId": vm.get("uuid"), "serverName": vm.get("name")},
                    f"Lịch sử hoạt động VM **{vm.get('name')}**")
        return ("resource_audit", None, "Bạn muốn xem lịch sử của VM nào?")

    # ── Tag resource ──────────────────────────────────────────────────────────
    if any(w in msg for w in ["thêm tag", "gắn tag", "tag cho", "add tag", "đặt tag"]):
        vm = find_vm(msg)
        tag_m = re.search(r'tag[:\s]+([^\s,;]+)', msg)
        tag_val = tag_m.group(1) if tag_m else None
        if vm and tag_val:
            return ("resource_tag",
                    {"serverId": vm.get("uuid"), "serverName": vm.get("name"), "tag": tag_val},
                    f"Thêm tag **{tag_val}** cho VM **{vm.get('name')}**")
        return ("resource_tag", None, "Cần biết tên VM và tag. VD: 'thêm tag env:prod cho vm-web'")

    # ── Resize Volume ─────────────────────────────────────────────────────────
    if any(w in msg for w in ["resize volume", "tăng dung lượng", "mở rộng volume", "extend volume", "tăng size volume", "nâng dung lượng"]):
        vol = find_volume(msg)
        size_m = re.search(r'(\d+)\s*(?:gb|GB|G)', message)
        new_size = int(size_m.group(1)) if size_m else None
        if vol and new_size:
            vol_name = vol.get("name") or vol.get("volumeName")
            cur_size = vol.get("size", 0)
            if new_size <= cur_size:
                return (None, None, f"Dung lượng mới ({new_size}GB) phải lớn hơn hiện tại ({cur_size}GB). Volume không thể thu nhỏ.")
            return ("volume_resize",
                    {"volumeId": vol.get("uuid"), "volumeName": vol_name, "size": new_size},
                    f"Tăng dung lượng Volume **{vol_name}** từ **{cur_size}GB** → **{new_size}GB**")
        hint = f"Volume **{vol.get('name')}** hiện có {vol.get('size','?')}GB. " if vol else ""
        return ("volume_resize", None, f"{hint}Vui lòng gõ: **tăng dung lượng [tên volume] lên [size]GB**")

    # ── SSH Key: tạo mới ─────────────────────────────────────────────────────
    if any(w in msg for w in ["tạo ssh key", "tạo key pair", "tạo keypair", "thêm ssh key", "generate key", "tạo key mới"]):
        key_m = re.search(r'(?:tên|name)\s+([\w\-\.]+)', message, re.IGNORECASE)
        if not key_m:
            words = [w for w in message.split() if len(w) > 3 and w not in ["tạo","ssh","key","pair","keypair","thêm","generate","mới"]]
            key_name = words[-1] if words else None
        else:
            key_name = key_m.group(1)
        if key_name:
            return ("sshkey_create",
                    {"name": key_name},
                    f"Tạo SSH Key Pair mới tên **{key_name}** (private key sẽ hiển thị 1 lần duy nhất)")
        return ("sshkey_create", None, "Bạn muốn đặt tên gì cho SSH Key? VD: **tạo ssh key tên deploy-key**")

    # ── SSH Key: xóa ─────────────────────────────────────────────────────────
    if any(w in msg for w in ["xóa ssh key", "xoá ssh key", "delete ssh key", "xóa keypair", "xóa key pair", "remove key"]):
        key_m = None
        for k in sshkeys:
            kname = (k.get("name") or "").lower()
            if kname and kname in msg:
                key_m = k
                break
        if key_m:
            return ("sshkey_delete",
                    {"keyId": key_m.get("id") or key_m.get("uuid"), "keyName": key_m.get("name")},
                    f"⚠️ XÓA SSH Key **{key_m.get('name')}** — không thể khôi phục!")
        return ("sshkey_delete", None, "Bạn muốn xóa SSH Key nào? Hỏi **liệt kê ssh key** để xem danh sách.")

    # ── SSH Key: liệt kê ─────────────────────────────────────────────────────
    if any(w in msg for w in ["liệt kê ssh", "xem ssh key", "danh sách key", "list ssh key", "ssh key nào", "các key"]):
        return ("list_sshkeys", {}, "Danh sách SSH Key Pairs")

    # ── Quota usage ───────────────────────────────────────────────────────────
    if any(w in msg for w in ["quota", "hạn mức", "giới hạn tài nguyên", "quota usage", "còn quota", "dùng bao nhiêu quota"]):
        return ("quota_usage", {}, "Xem hạn mức sử dụng tài nguyên")

    # ── List flavors ──────────────────────────────────────────────────────────
    if any(w in msg for w in ["liệt kê flavor", "xem flavor", "danh sách flavor", "flavor nào", "list flavor", "các flavor"]):
        return ("list_flavors", {}, "Danh sách flavor khả dụng")

    # ── List images ───────────────────────────────────────────────────────────
    if any(w in msg for w in ["liệt kê image", "xem image", "danh sách image", "image nào",
                               "list image", "các image", "os nào", "hệ điều hành nào",
                               "có những os", "có những image", "supported os", "hỗ trợ os"]):
        return ("list_images", {}, "Danh sách image khả dụng")

    # ── List volume types ─────────────────────────────────────────────────────
    if any(w in msg for w in ["liệt kê volume type", "danh sách volume type", "volume type nào",
                               "loại volume", "storage type", "nvme", "ssd type"]):
        return ("list_volume_types", {}, "Danh sách volume type khả dụng")

    return (None, None, None)


def resolve_vm_create_params(message, flavors, images, subnets, networks, sshkeys, vol_types,
                              region="HCM", zone="HCM03-1A"):
    """
    Parse a free-text VM creation request and resolve human-readable names → real IDs.
    Uses static reference data (references/) for images/flavors/vol-types to guarantee
    correct IDs. Subnets/networks/sshkeys still come from live API.
    Returns (params_dict, None) on success, or (None, error_message) when info is missing.
    """
    msg = message.lower()

    # ── VM name ───────────────────────────────────────────────────────────────
    name_m = re.search(r'(?:tên|name)[:\s]+"([^"]+)"|(?:tên|name)[:\s]+([^\s,;|]+)', message, re.IGNORECASE)
    vm_name = (name_m.group(1) or name_m.group(2)) if name_m else None
    if not vm_name:
        SKIP = {"tạo","tao","tên","ten","vm","server","may","máy","flavor","os",
                "ubuntu","centos","windows","debian","rocky","alma","rhel","debian",
                "subnet","network","ssh","key","disk","floating","vcpu","cpu","core",
                "gb","ram","create","tao","new","moi","mới"}
        for w in message.split():
            if re.match(r'^[a-zA-Z][a-zA-Z0-9\-_.]{1,}$', w) and w.lower() not in SKIP:
                vm_name = w; break
    if vm_name:
        vm_name = re.sub(r'[^a-zA-Z0-9.\-]', '-', vm_name)
        vm_name = re.sub(r'-+', '-', vm_name).strip('-')
        if len(vm_name) < 5: vm_name = (vm_name + '-----')[:5]
        vm_name = vm_name[:242]

    # ── Flavor — from static reference, match by cpu+ram spec ────────────────
    ref_flavs = ref_flavors(region, zone)
    # prefer s2/preferred flavors
    preferred = [f for f in ref_flavs if f.get("preferred") and not f.get("deprecated")]
    search_flavs = preferred if preferred else ref_flavs

    flavor = None
    cpu_m = re.search(r'(\d+)\s*(?:vcpu|cpu|core)', msg)
    ram_m = re.search(r'(\d+)\s*gb(?!\s*(?:disk|root|ssd|hdd|ổ|storage))', msg)
    if cpu_m or ram_m:
        want_cpu = int(cpu_m.group(1)) if cpu_m else None
        want_ram = int(ram_m.group(1)) if ram_m else None
        for f in search_flavs:
            cpu_ok = (want_cpu is None) or f["cpu"] == want_cpu
            ram_ok = (want_ram is None) or f["ram_gb"] == want_ram
            if cpu_ok and ram_ok:
                flavor = f; break
        # relax: match cpu only if ram not found
        if not flavor and want_cpu:
            for f in search_flavs:
                if f["cpu"] == want_cpu:
                    flavor = f; break
    if not flavor:
        # name match
        for f in search_flavs:
            if f["name"].lower() in msg:
                flavor = f; break

    # ── Image — from static reference ────────────────────────────────────────
    ref_imgs = ref_images(region, zone)
    image = None
    OS_PATTERNS = [
        ("ubuntu",     r'ubuntu[\s\-_]*([\d.]+)?'),
        ("centos",     r'centos[\s\-_]*([\d.]+)?'),
        ("windows",    r'windows[\s\-_]*(server[\s\-_]*)?([\d.]+)?'),
        ("debian",     r'debian[\s\-_]*([\d.]+)?'),
        ("rocky",      r'rocky[\s\-_]*([\d.]+)?'),
        ("almalinux",  r'alma[\s\-_]*([\d.]+)?'),
        ("rhel",       r'rhel[\s\-_]*([\d.]+)?'),
        ("opensuse",   r'opensuse[\s\-_]*([\d.]+)?'),
        ("oracle",     r'oracle[\s\-_]*([\d.]+)?'),
    ]
    for os_kw, pat in OS_PATTERNS:
        if os_kw not in msg and os_kw.replace("linux","") not in msg:
            continue
        ver_m = re.search(pat, msg)
        # extract version number from last group
        version = None
        if ver_m:
            for g in reversed(ver_m.groups()):
                if g and re.search(r'\d', g):
                    version = g.strip(); break
        # Score each matching image
        best, best_score = None, -1
        for i in ref_imgs:
            iname = i["name"].lower()
            ios   = i.get("os","").lower()
            if os_kw not in iname and os_kw not in ios:
                continue
            score = 0
            if version and version in iname: score += 10
            if i.get("recommended"):         score += 5
            if "uefi" not in iname:          score += 1   # prefer non-UEFI when not specified
            if score > best_score:
                best, best_score = i, score
        if best:
            image = best; break

    if not image:
        # generic fallback: any image whose name tokens appear in message
        for i in ref_imgs:
            words = [w for w in re.split(r'[\s\-_.]', i["name"].lower()) if len(w) > 3]
            if words and all(w in msg for w in words[:2]):
                image = i; break

    # ── Subnet — from live API ────────────────────────────────────────────────
    subnet = None
    for s in subnets:
        if (s.get("name") or "").lower() in msg:
            subnet = s; break
    if not subnet and subnets:
        subnet = subnets[0]

    network = None
    if subnet:
        net_id = subnet.get("networkId") or subnet.get("networkUuid")
        network = next((n for n in networks
                        if n.get("uuid") == net_id or n.get("id") == net_id), None)

    # ── SSH key ───────────────────────────────────────────────────────────────
    sshkey = None
    for k in sshkeys:
        if (k.get("name") or "").lower() in msg:
            sshkey = k; break

    # ── Root disk size ────────────────────────────────────────────────────────
    disk_m = re.search(r'(?:disk|root|ổ\s*cứng|storage)[^\d]*(\d+)\s*gb', msg)
    if not disk_m:
        disk_m = re.search(r'(\d+)\s*gb[^\w]*(?:disk|root|ổ)', msg)
    root_disk = int(disk_m.group(1)) if disk_m else 40

    # ── Volume type — from static reference ──────────────────────────────────
    ref_vts = ref_vol_types(region, zone)
    vol_type = next((v for v in ref_vts if v.get("default")), ref_vts[0] if ref_vts else {})

    # ── Floating IP ───────────────────────────────────────────────────────────
    want_fip = (any(w in msg for w in ["floating", "wan", "public ip", "ip công cộng"])
                and "không" not in msg)

    # ── Validation — subnet is optional (GreenNode auto-assigns default) ────────
    missing = []
    if not vm_name: missing.append("**tên VM**")
    if not flavor:  missing.append("**flavor** (ví dụ: 2vCPU 4GB)")
    if not image:   missing.append("**hệ điều hành** (Ubuntu 22.04, CentOS 7…)")
    # subnet NOT required — GreenNode project always has a default VPC/subnet
    if missing:
        return None, f"Còn thiếu: {', '.join(missing)}."

    _net_id    = (subnet.get("networkId") or subnet.get("networkUuid")
                  or (network.get("uuid") if network else "")) if subnet else ""
    _subnet_id = (subnet.get("id") or subnet.get("uuid", "")) if subnet else ""
    _subnet_nm = subnet.get("name", "default") if subnet else "default"

    print(f"[RESOLVE] name={vm_name} flavorId={flavor['id']} imageId={image['id']} "
          f"subnet={'OK:'+_subnet_nm if subnet else 'EMPTY(will use default)'}")

    return {
        "name":           vm_name,
        "flavorId":       flavor["id"],
        "flavorName":     flavor["name"],
        "imageId":        image["id"],
        "imageName":      image["name"],
        "networkId":      _net_id,
        "subnetId":       _subnet_id,
        "subnetName":     _subnet_nm,
        "rootDiskSize":   root_disk,
        "rootDiskTypeId": vol_type.get("id", ""),
        "sshKeyId":       (sshkey.get("id") or sshkey.get("uuid")) if sshkey else None,
        "secgroupIds":    [],
        "attachFloating": want_fip,
    }, None


def execute_vm_action(token, uid, project_id, action_type, params):
    """Execute start/stop/reboot — return immediately, GreenNode processes async."""
    P         = project_id
    server_id = params.get("serverId")
    if not server_id:
        return False, "Không tìm thấy server ID", None

    # Exact endpoints from VNG Cloud API docs
    ENDPOINT = {
        "vm_stop":   ("PUT", f"v2/{P}/servers/{server_id}/stop",   None),
        "vm_start":  ("PUT", f"v2/{P}/servers/{server_id}/start",  None),
        "vm_reboot": ("PUT", f"v2/{P}/servers/{server_id}/reboot", {"type": "SOFT"}),
    }

    method, path, body = ENDPOINT[action_type]
    status, data = gn_api(token, uid, method, path, body)

    if status not in (200, 201, 202, 204):
        return False, f"GreenNode lỗi {status}: {data}", None

    # Return success immediately — GreenNode processes async in background
    return True, None, {"status": "PROCESSING", "message": "Lệnh đã được gửi, GreenNode đang xử lý"}

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
        def _parse_api(status, data):
            """Safely extract list from any GreenNode API response shape."""
            if status not in (200, 201): return []
            if isinstance(data, list): return data
            if isinstance(data, dict):
                for k in ("listData","data","items","results","servers","volumes",
                          "networks","subnets","flavors","images","sshKeys","volumeTypes"):
                    if isinstance(data.get(k), list): return data[k]
            return []

        # Fetch flavors, images, subnets, SSH keys, volume types for VM creation
        sf, df   = gn_api(token, uid, "GET", f"v2/{P}/flavors")
        flavors  = _parse_api(sf, df)
        si, di   = gn_api(token, uid, "GET", f"v2/{P}/images")
        images   = _parse_api(si, di)
        ssu, dsu = gn_api(token, uid, "GET", f"v2/{P}/subnets")
        subnets  = _parse_api(ssu, dsu)
        ssk, dsk = gn_api(token, uid, "GET", f"v2/{P}/sshkeys")
        sshkeys  = _parse_api(ssk, dsk)
        svt, dvt = gn_api(token, uid, "GET", f"v2/{P}/volume-types")
        vol_types = _parse_api(svt, dvt)

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

    # ── Format creation resources for LLM context ────────────────────────────
    # Flavors, images, vol-types: use static reference data (authoritative IDs)
    _ref_flavs = ref_flavors()   # all 68 flavors
    _ref_imgs  = ref_images()    # all 43 images
    _ref_vts   = ref_vol_types() # all 6 vol types

    def fmt_subnet(s):
        sid   = s.get("id") or s.get("uuid","?")
        name  = s.get("name","?")
        netid = s.get("networkId") or s.get("networkUuid","?")
        cidr  = s.get("cidr","?")
        return f"SUBNET|{sid}|{name}|net:{netid}|cidr:{cidr}"

    def fmt_sshkey(k):
        kid  = k.get("id") or k.get("uuid","?")
        name = k.get("name","?")
        return f"SSHKEY|{kid}|{name}"

    # Only show preferred/non-deprecated flavors in context to save tokens
    _ctx_flavs = [f for f in _ref_flavs if f.get("preferred") and not f.get("deprecated")]
    flavor_lines = "\n".join(
        f"FLAVOR|{f['id']}|{f['name']}|{f['cpu']}vCPU|{f['ram_gb']}GB|{f['family']}"
        for f in _ctx_flavs
    ) or "(none)"

    # Group images by OS family for context
    _img_by_os: dict = {}
    for i in _ref_imgs:
        _img_by_os.setdefault(i["os"], []).append(i)
    image_lines = "\n".join(
        f"IMAGE|{i['id']}|{i['name']}|{i['os']}"
        + ("|recommended" if i.get("recommended") else "")
        for os_family in _img_by_os
        for i in _img_by_os[os_family]
    ) or "(none)"

    voltype_lines = "\n".join(
        f"VOLTYPE|{v['id']}|{v['name']}|{v['iops']}IOPS"
        + ("|default" if v.get("default") else "")
        for v in _ref_vts
    ) or "(none)"

    subnet_lines  = "\n".join(fmt_subnet(s) for s in subnets) or "(none)"
    sshkey_lines  = "\n".join(fmt_sshkey(k) for k in sshkeys) or "(none)"

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
{fip_lines}

--- Flavor ({len(_ctx_flavs)} preferred, {len(_ref_flavs)} total) [dùng cho tạo/resize VM] ---
{flavor_lines}

--- Image ({len(_ref_imgs)} total) [dùng cho tạo VM] ---
{image_lines}

--- Subnet ({len(subnets)}) [dùng cho tạo VM] ---
{subnet_lines}

--- SSH Key ({len(sshkeys)}) [dùng cho tạo VM] ---
{sshkey_lines}

--- Volume Type ({len(_ref_vts)}) [dùng cho tạo VM/Volume] ---
{voltype_lines}"""

    system_prompt = f"""Bạn là GreenNode AI Assistant — trợ lý quản lý hạ tầng đám mây thông minh cho GreenNode (VNG Cloud) HCM-3.
Dữ liệu bên dưới được lấy REAL-TIME từ GreenNode API ngay lúc user gửi tin nhắn — luôn chính xác và mới nhất.

{context}

HƯỚNG DẪN TRẢ LỜI:
- Trả lời bằng tiếng Việt, ngắn gọn và chính xác
- Dùng Markdown: **bold**, table, bullet list
- Trạng thái VM: 🟢 ACTIVE · 🔴 SHUTOFF · 🟡 BUILD · ⚪ khác
- Phát hiện vấn đề: ⚠️ orphan resource, 🚨 security risk, ❌ lỗi
- Khi user muốn thực hiện action trên hạ tầng (bất kể cách diễn đạt), trả về JSON đặc biệt:
  {{"__action__": "<loại action>", "params": {{...}}, "desc": "<mô tả ngắn>"}}
  Các loại action và params:
  - vm_start/vm_stop/vm_reboot: {{"serverId": "uuid", "serverName": "tên"}}
  - volume_attach: {{"serverId": "uuid", "serverName": "tên", "volumeId": "uuid", "volumeName": "tên", "zoneId": "uuid"}}
  - volume_detach: {{"serverId": "uuid", "serverName": "tên", "volumeId": "uuid", "volumeName": "tên"}}
  - fip_associate: {{"serverId": "uuid", "serverName": "tên", "floatingIp": "ip"}}
  - fip_disassociate: {{"serverId": "uuid", "serverName": "tên"}}
  - vm_rename: {{"serverId": "uuid", "serverName": "tên", "newName": "tên mới"}}
  - volume_rename: {{"volumeId": "uuid", "volumeName": "tên", "newName": "tên mới"}}
  - sg_attach/sg_detach: {{"serverId": "uuid", "serverName": "tên", "sgIds": ["uuid"]}}
  - sg_rule_add: {{"sgId": "uuid", "sgName": "tên", "rule": {{"direction": "ingress", "etherType": "IPv4", "portRangeMin": 80, "portRangeMax": 80, "protocol": "tcp", "remoteIpPrefix": "0.0.0.0/0"}}}}
  - vm_snapshot: {{"serverId": "uuid", "serverName": "tên", "snapshotName": "tên"}}
  - vm_resize: {{"serverId": "uuid", "serverName": "tên", "flavorId": "flav-xxx"}}
  - vm_delete: {{"serverId": "uuid", "serverName": "tên"}}
  - volume_create: {{"name": "tên", "size": 20, "volumeTypeId": "vtype-xxx"}}
  - volume_delete: {{"volumeId": "uuid", "volumeName": "tên"}}
  - volume_resize: {{"volumeId": "uuid", "volumeName": "tên", "size": 50}}
  - sshkey_create: {{"name": "tên-key"}}
  - sshkey_delete: {{"keyId": "uuid", "keyName": "tên"}}
  - vm_create: {{"name": "tên", "flavorId": "flav-xxx", "imageId": "img-xxx", "networkId": "net-xxx", "subnetId": "sub-xxx", "rootDiskSize": 20, "rootDiskTypeId": "vtype-xxx", "sshKeyId": "key-xxx hoặc null", "secgroupIds": [], "attachFloating": false, "flavorName": "tên flavor", "imageName": "tên image"}}
  ⚠️ QUAN TRỌNG:
  - Chỉ trả về JSON thuần duy nhất, KHÔNG có text hay markdown xung quanh.
  - Dùng đúng ID từ dữ liệu context (FLAVOR|ID|..., IMAGE|ID|..., SUBNET|ID|..., SSHKEY|ID|..., VOLTYPE|ID|...)
  - Với vm_create: thêm trường "flavorName" và "imageName" để hiển thị confirm rõ ràng
  - Nếu thiếu thông tin quan trọng (tên VM, OS), hỏi lại user thay vì đoán
  - Nếu user chỉ nói "tạo VM" mà không có chi tiết, hỏi: tên VM, OS muốn dùng, cấu hình (flavor)
  - ⛔ TUYỆT ĐỐI KHÔNG được bịa đặt hoặc tự suy luận ID (flav-xxx, img-xxx, vtype-xxx...)
  - ⛔ Nếu context KHÔNG có dữ liệu FLAVOR/IMAGE/SUBNET, hãy nói thẳng: "Hệ thống chưa lấy được danh sách flavor/image. Bạn hãy hỏi **liệt kê flavor** hoặc **liệt kê image** để xem ID thực."
  - ✅ Chỉ dùng ID có trong phần context (dòng bắt đầu bằng FLAVOR|, IMAGE|, SUBNET|...)

QUAN TRỌNG — ĐỘ TRỄ TRẠNG THÁI:
GreenNode API nhận lệnh ngay lập tức nhưng việc thực thi thực tế cần 30-120 giây.
Nếu user vừa stop/start/reboot VM và hỏi lại trạng thái ngay:
- Nếu dữ liệu real-time vẫn hiện ACTIVE sau lệnh stop → đây là bình thường, server đang trong quá trình dừng
- KHÔNG nói "đã dừng thành công" nếu dữ liệu thực tế vẫn là ACTIVE
- Hãy nói: "Lệnh đã được gửi. GreenNode đang xử lý — vui lòng chờ 1-2 phút rồi kiểm tra lại"
- Nếu sau 2 phút vẫn không đổi trạng thái → có thể có lỗi, user nên kiểm tra trên portal

DỮ LIỆU REAL-TIME được cập nhật mỗi lần user gửi tin nhắn."""

    # 3. Detect action intent — execute DIRECTLY without asking LLM
    confirmed      = body.get("confirmed", False)
    pending_action = body.get("pendingAction", None)

    # ── Early VM-create resolution (server-side, bypasses LLM) ───────────────
    # Fires when message looks like a VM spec (has OS + flavor/cpu/gb clues)
    _msg_lo = user_message.lower()
    _has_os     = any(w in _msg_lo for w in ["ubuntu","centos","windows","debian","rocky","alma"])
    _has_size   = bool(re.search(r'\d+\s*(?:vcpu|cpu|core|\d+\s*gb)', _msg_lo))
    _has_name   = bool(re.search(r'(?:tên|name)[:\s]+\S', user_message, re.IGNORECASE)
                       or re.search(r'\b[a-z][a-z0-9\-]{2,}\b', _msg_lo))
    if _has_os and (_has_size or _has_name) and not confirmed:
        _params, _err = resolve_vm_create_params(
            user_message, flavors, images, subnets, networks, sshkeys, vol_types)
        if _params:
            # All IDs resolved — go straight to confirm screen
            _desc = f"Tạo VM **{_params['name']}** — {_params['flavorName']} · {_params['imageName']}"
            ssh_name    = next((k.get("name","?") for k in sshkeys
                                if k.get("id") == _params.get("sshKeyId")
                                or k.get("uuid") == _params.get("sshKeyId")), "_(không có)_")
            subnet_name = (_params.get("subnetName") or _params.get("subnetId") or
                           "_(default VPC — tự động)_")
            spec = (
                f"🖥️ **Xác nhận tạo VM mới**\n\n"
                f"| Thông số | Giá trị |\n|---|---|\n"
                f"| Tên | **{_params['name']}** |\n"
                f"| Flavor | {_params['flavorName']} (`{_params['flavorId']}`) |\n"
                f"| OS / Image | {_params['imageName']} |\n"
                f"| Subnet | {subnet_name} |\n"
                f"| Root disk | {_params['rootDiskSize']} GB |\n"
                f"| SSH Key | {ssh_name} |\n"
                f"| Floating IP | {'Có' if _params.get('attachFloating') else 'Không'} |\n\n"
                f"⚠️ VM sẽ được tạo và **tính phí ngay lập tức**. Xác nhận?"
            )
            return jsonify({
                "reply":         spec,
                "fetchedAt":     now,
                "needConfirm":   True,
                "pendingAction": {"type": "vm_create", "params": _params, "desc": _desc},
            })
        else:
            # Resolution failed — check if it's specifically the subnet missing
            # Generic missing info (name/flavor/image) — guide user
            _ref_flav_hint = "\n".join(
                f"  • **{f['name']}** — {f['cpu']} vCPU / {f['ram_gb']} GB"
                for f in ref_flavors() if f.get("preferred"))[:300]
            _ref_img_hint = "\n".join(
                f"  • **{i['name']}**"
                for i in ref_images() if i.get("recommended") or "22.04" in i["name"])[:200]
            reply = (
                f"⚠️ **Chưa đủ thông tin để tạo VM**: {_err}\n\n"
                f"**Flavor phổ biến** (S2 generation):\n{_ref_flav_hint}\n\n"
                f"**Image phổ biến**:\n{_ref_img_hint}\n\n"
                f"💡 Thử lại với đầy đủ thông tin, ví dụ:\n"
                f"> `tạo vm tên web-01 ubuntu 22.04 2vcpu 4gb`"
            )
            return jsonify({"reply": reply, "fetchedAt": now})

    if confirmed and pending_action:
        # User confirmed → execute the action NOW
        action_type = pending_action.get("type")
        params      = pending_action.get("params", {})
        print(f"[CONFIRM] type={action_type} params={params}")
        desc        = pending_action.get("desc", "")
        server_name = params.get("serverName", "VM")

        if action_type in ("vm_stop", "vm_start", "vm_reboot"):
            ok, err, _ = execute_vm_action(token, uid, project_id, action_type, params)
            action_labels = {"vm_stop": "🔴 tắt", "vm_start": "🟢 khởi động", "vm_reboot": "🔄 khởi động lại"}
            label = action_labels.get(action_type, "thực hiện")
            db_write_audit(customer_name, project_id, action_type, server_name, params,
                           'success' if ok else 'failed',
                           f"Lệnh {label} VM {server_name}" if ok else str(err))
            if ok:
                reply = f"✅ Đã gửi lệnh **{label}** VM **{server_name}**.\n\n⏳ GreenNode đang xử lý — chờ 1-2 phút rồi hỏi lại để kiểm tra trạng thái thực tế."
            else:
                reply = f"❌ **Thất bại:** {err}\n\nVui lòng thử lại hoặc kiểm tra trên GreenNode portal."
            return jsonify({"reply": reply, "fetchedAt": now, "actionDone": True})

        # ── vm_create: always re-resolve IDs from static reference data ────────
        if action_type == "vm_create":
            _spec = (f"tạo vm tên {params.get('name','')} "
                     f"{params.get('imageName', params.get('imageId',''))} "
                     f"{params.get('flavorName', params.get('flavorId',''))} "
                     f"{params.get('subnetName','')}")
            _re_params, _re_err = resolve_vm_create_params(
                _spec, flavors, images, subnets, networks, sshkeys, vol_types)
            if _re_params:
                params = {**params, **_re_params}
                print(f"[VM_CREATE_RESOLVED] {params}")
            else:
                # Validate existing IDs against static reference
                _ref_img_ids = {i["id"] for i in ref_images()}
                _ref_flv_ids = {f["id"] for f in ref_flavors()}
                _iid = str(params.get("imageId", ""))
                _fid = str(params.get("flavorId", ""))
                _bad = []
                if _iid not in _ref_img_ids: _bad.append(f"imageId `{_iid}` không hợp lệ")
                if _fid not in _ref_flv_ids: _bad.append(f"flavorId `{_fid}` không hợp lệ")
                if _bad:
                    reply = (f"❌ Không thể tạo VM — {'; '.join(_bad)}.\n\n"
                             f"Hãy thử lại, ví dụ:\n"
                             f"> tạo vm tên my-server ubuntu 22.04 2vcpu 4gb")
                    return jsonify({"reply": reply, "fetchedAt": now, "actionDone": True})

        # Handle confirmed volume/FIP/SG actions
        EXTENDED_CONFIRM = {"volume_attach","volume_detach","fip_associate","fip_disassociate","sg_attach","sg_detach","vm_rename","volume_rename","sg_rule_add","sg_rule_remove","vm_snapshot","vm_create","vm_resize","vm_delete","volume_create","volume_delete","volume_resize","sshkey_create","sshkey_delete"}
        if action_type in EXTENDED_CONFIRM:
            ok, data = execute_extended_action(token, uid, project_id, action_type, params)
            labels = {
                "volume_attach":    f"Đã gắn volume **{params.get('volumeName','?')}** vào VM **{params.get('serverName','?')}**",
                "volume_detach":    f"Đã gỡ volume **{params.get('volumeName','?')}** khỏi VM **{params.get('serverName','?')}**",
                "volume_resize":    f"Đã tăng dung lượng Volume **{params.get('volumeName','?')}** lên **{params.get('size','?')}GB**",
                "sshkey_create":    f"Đã tạo SSH Key Pair **{params.get('name','?')}**",
                "sshkey_delete":    f"Đã xóa SSH Key **{params.get('keyName','?')}**",
                "fip_associate":    f"Đã gắn Floating IP **{params.get('floatingIp','?')}** vào VM **{params.get('serverName','?')}**",
                "fip_disassociate": f"Đã gỡ Floating IP khỏi VM **{params.get('serverName','?')}**",
                "sg_attach":        f"Đã gắn Security Group vào VM **{params.get('serverName','?')}**",
                "sg_detach":        f"Đã gỡ Security Group khỏi VM **{params.get('serverName','?')}**",
                "vm_rename":        f"Đã đổi tên VM **{params.get('serverName','?')}** thành **{params.get('newName','?')}**",
                "volume_rename":    f"Đã đổi tên Volume thành **{params.get('newName','?')}**",
                "vm_snapshot":      f"Đã tạo snapshot VM **{params.get('serverName','?')}**",
                "vm_create":        f"Đã gửi lệnh tạo VM **{params.get('name','?')}** ({params.get('flavorName','?')} · {params.get('imageName','?')}).\n\n⏳ GreenNode đang khởi tạo — thường mất 2-5 phút. Hỏi lại để kiểm tra trạng thái.",
                "vm_resize":        f"Đã resize VM **{params.get('serverName','?')}** sang flavor **{params.get('flavorName','?')}**.\n\n⏳ GreenNode đang xử lý — chờ 1-2 phút.",
                "vm_delete":        f"Đã xóa VM **{params.get('serverName','?')}**",
                "volume_create":    f"Đã tạo Volume **{params.get('name','?')}**",
                "volume_delete":    f"Đã xóa Volume **{params.get('volumeName','?')}**",
            }
            # Audit log
            resource_name = (params.get("serverName") or params.get("volumeName") or
                             params.get("name") or params.get("keyName") or "?")
            db_write_audit(customer_name, project_id, action_type, resource_name, params,
                           'success' if ok else 'failed',
                           labels.get(action_type,"") if ok else str(data))
            if ok:
                # SSH key create: show private key (only shown once!)
                if action_type == "sshkey_create":
                    priv_key = (data or {}).get("privateKey") or (data or {}).get("private_key") or ""
                    pub_key  = (data or {}).get("publicKey")  or (data or {}).get("public_key")  or ""
                    key_name = params.get("name","?")
                    reply = (f"✅ Đã tạo SSH Key Pair **{key_name}**\n\n"
                             f"⚠️ **Lưu private key ngay — chỉ hiển thị 1 lần duy nhất!**\n\n")
                    if priv_key:
                        reply += f"```\n{priv_key}\n```\n"
                    if pub_key:
                        reply += f"\n**Public Key:**\n```\n{pub_key}\n```"
                    if not priv_key and not pub_key:
                        reply += f"Key đã tạo. Vui lòng tải về từ GreenNode portal."
                else:
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
                        run_at,
                        customer=customer_name
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

            # ── SG rules list ─────────────────────────────────────────────────
            if action_type == "sg_list_rules":
                sg_id   = params.get("sgId")
                sg_name = params.get("sgName", "?")
                s, d = gn_api(token, uid, "GET", f"v2/{P}/secgroups/{sg_id}")
                if s == 200:
                    rules = (d.get("secgroupRuleEntities")
                             or d.get("rules")
                             or d.get("data", {}).get("secgroupRuleEntities", [])
                             or [])
                    if not rules:
                        return jsonify({"reply": f"📋 Security Group **{sg_name}** chưa có rule nào.", "fetchedAt": now})
                    lines = ["| Rule ID (8 ký tự đầu) | Chiều | Proto | Port | CIDR |",
                             "|---|---|---|---|---|"]
                    for r in rules:
                        rid    = r.get("id") or r.get("uuid") or "?"
                        short  = str(rid)[:8]
                        direct = r.get("direction", "?")
                        proto  = r.get("protocol") or "all"
                        pmin   = r.get("portRangeMin", "")
                        pmax   = r.get("portRangeMax", "")
                        port_s = f"{pmin}-{pmax}" if pmin and pmax and pmin != pmax else str(pmin) if pmin else "all"
                        cidr   = r.get("remoteIpPrefix") or "0.0.0.0/0"
                        lines.append(f"| `{short}` | {direct} | {proto} | {port_s} | {cidr} |")
                    reply  = f"🛡️ **Rules của SG {sg_name}** ({len(rules)} rules):\n\n" + "\n".join(lines)
                    reply += "\n\n💡 Để xóa rule: **xóa rule [UUID đầy đủ] sg [tên SG]**"
                    return jsonify({"reply": reply, "fetchedAt": now})
                return jsonify({"reply": f"❌ Không lấy được rules (status {s}).", "fetchedAt": now})

            # ── Audit / activity log ──────────────────────────────────────────
            if action_type == "resource_audit":
                server_id   = params.get("serverId")
                server_name = params.get("serverName", "?")
                s, d = gn_api(token, uid, "GET", f"v2/{P}/servers/{server_id}/events")
                if s != 200:
                    s, d = gn_api(token, uid, "GET", f"v2/{P}/servers/{server_id}/actions")
                if s == 200:
                    events = (d.get("listData") or d.get("events") or d.get("actions") or [])[:20]
                    if not events:
                        return jsonify({"reply": f"📋 Chưa có sự kiện nào cho VM **{server_name}**.", "fetchedAt": now})
                    lines = []
                    for e in events:
                        ts     = str(e.get("createdAt") or e.get("startTime") or e.get("timestamp", ""))[:19].replace("T", " ")
                        action = e.get("action") or e.get("event") or e.get("type", "?")
                        user   = e.get("userId") or e.get("user") or e.get("requestId", "system")
                        result = str(e.get("result") or e.get("status", "")).upper()
                        icon   = "✅" if result in ("SUCCESS","ACTIVE","DONE") else ("❌" if result in ("ERROR","FAILED") else "⏳")
                        lines.append(f"• {icon} `{ts}` — **{action}** ({user})")
                    reply = f"📋 **Lịch sử VM {server_name}** (20 sự kiện gần nhất):\n\n" + "\n".join(lines)
                    return jsonify({"reply": reply, "fetchedAt": now})
                return jsonify({"reply": f"⚠️ Không lấy được lịch sử (status {s}). API có thể chưa hỗ trợ trên HCM-3.", "fetchedAt": now})

            # ── Quota usage ───────────────────────────────────────────────────
            if action_type == "quota_usage":
                s, d = gn_api(token, uid, "GET", f"v2/{P}/limits")
                if s == 403:
                    return jsonify({"reply": (
                        "⚠️ **Không có quyền xem quota** (HTTP 403).\n\n"
                        "Cần bổ sung IAM policy cho Service Account:\n"
                        "- `vServerFullAccess` hoặc `vServerReadOnly`\n\n"
                        "Liên hệ admin GreenNode để cấp quyền."
                    ), "fetchedAt": now})
                if s == 200:
                    limits = d.get("limits") or d.get("listData") or d.get("data") or {}
                    if isinstance(limits, list) and limits:
                        lines = ["| Tài nguyên | Đang dùng | Giới hạn | % |", "|---|---|---|---|"]
                        for x in limits:
                            name  = x.get("resource") or x.get("name","?")
                            used  = x.get("inUse") or x.get("used", 0)
                            limit = x.get("limit") or x.get("maxAllowed", "∞")
                            pct   = f"{int(used)/int(limit)*100:.0f}%" if str(limit).isdigit() and int(limit) > 0 else "—"
                            lines.append(f"| {name} | {used} | {limit} | {pct} |")
                        return jsonify({"reply": "📊 **Quota sử dụng:**\n\n" + "\n".join(lines), "fetchedAt": now})
                    elif isinstance(limits, dict) and limits:
                        lines = ["| Tài nguyên | Giá trị |", "|---|---|"]
                        for k, v in limits.items():
                            lines.append(f"| {k} | {v} |")
                        return jsonify({"reply": "📊 **Quota sử dụng:**\n\n" + "\n".join(lines), "fetchedAt": now})
                return jsonify({"reply": f"⚠️ Không lấy được quota (status {s}).", "fetchedAt": now})

            # ── List flavors (from static reference) ─────────────────────────
            if action_type == "list_flavors":
                all_flavs = ref_flavors()
                # Group by family
                families: dict = {}
                for f in sorted(all_flavs, key=lambda x: (x["cpu"], x["ram_gb"])):
                    fam = f["family"].upper()
                    families.setdefault(fam, []).append(f)
                lines = []
                for fam in ["GENERAL", "STANDARD", "HIGHMEM", "HIGHCPU"]:
                    if fam not in families: continue
                    lines.append(f"\n**{fam}**")
                    lines.append("| Tên | vCPU | RAM | Network | Giá ước tính/tháng | Gen |")
                    lines.append("|---|---|---|---|---|---|")
                    for f in families[fam]:
                        gen_badge  = "⭐ S2" if f.get("preferred") else ("~~S1~~" if f.get("deprecated") else "S2")
                        price_str  = f"{f['price_vnd']:,}đ".replace(",", ".") if f.get("price_vnd") else "—"
                        lines.append(f"| {f['name']} | {f['cpu']} | {f['ram_gb']} GB | {f.get('network','?')} | ~{price_str} | {gen_badge} |")
                reply = f"⚡ **Danh sách Flavor** ({len(all_flavs)} total, ⭐ = S2 preferred)\n" + "\n".join(lines)
                reply += "\n\n> 💡 Giá ước tính dựa theo bảng giá công khai VNG Cloud — chỉ mang tính tham khảo."
                reply += "\n\n💡 Tạo VM: `tạo vm tên [tên] ubuntu 22.04 2vcpu 4gb`"
                reply += "\n💡 Resize VM: `resize vm [tên VM] sang 4vcpu 8gb`"
                return jsonify({"reply": reply, "fetchedAt": now})

            # ── List images (from static reference) ──────────────────────────
            if action_type == "list_images":
                all_imgs = ref_images()
                by_os: dict = {}
                for i in all_imgs:
                    by_os.setdefault(i["os"], []).append(i)
                lines = []
                for os_fam in sorted(by_os.keys()):
                    lines.append(f"\n**{os_fam}**")
                    lines.append("| Image ID | Tên |")
                    lines.append("|---|---|")
                    for i in by_os[os_fam]:
                        badge = " ⭐" if i.get("recommended") else ""
                        lines.append(f"| `{i['id']}` | {i['name']}{badge} |")
                reply = f"🖼️ **Danh sách Image** ({len(all_imgs)} total, ⭐ = recommended)\n" + "\n".join(lines)
                reply += "\n\n💡 Tạo VM: `tạo vm tên [tên] ubuntu 22.04 2vcpu 4gb`"
                return jsonify({"reply": reply, "fetchedAt": now})

            # ── List volume types (from static reference) ─────────────────────
            if action_type == "list_volume_types":
                all_vts = ref_vol_types()
                lines = ["| Volume Type ID | Tên | IOPS | Throughput |", "|---|---|---|---|"]
                for v in all_vts:
                    badge = " ✅ default" if v.get("default") else ""
                    lines.append(f"| `{v['id']}` | {v['name']}{badge} | {v.get('iops','?')} | — |")
                reply = f"💾 **Danh sách Volume Type** ({len(all_vts)} types):\n\n" + "\n".join(lines)
                reply += "\n\n💡 Tạo volume: `tạo volume 100gb tên my-vol`"
                return jsonify({"reply": reply, "fetchedAt": now})

            # ── List SSH Keys ─────────────────────────────────────────────────
            if action_type == "list_sshkeys":
                if not sshkeys:
                    return jsonify({"reply": "🔑 Chưa có SSH Key nào. Gõ **tạo ssh key tên [tên]** để tạo mới.", "fetchedAt": now})
                lines = ["| Tên | ID | Fingerprint |", "|---|---|---|"]
                for k in sshkeys:
                    kid  = k.get("id") or k.get("uuid") or "?"
                    kname = k.get("name") or "?"
                    fp   = k.get("fingerprint") or k.get("publicKey","")[:30] + "…" if k.get("publicKey") else "—"
                    lines.append(f"| **{kname}** | `{kid}` | `{fp}` |")
                reply = f"🔑 **SSH Key Pairs** ({len(sshkeys)} keys):\n\n" + "\n".join(lines)
                reply += "\n\n💡 Tạo key mới: `tạo ssh key tên my-key`\n💡 Xóa key: `xóa ssh key [tên]`"
                return jsonify({"reply": reply, "fetchedAt": now})

            # ── Tag resource (low-risk — no confirm) ─────────────────────────
            if action_type == "resource_tag" and params:
                ok, data = execute_extended_action(token, uid, project_id, action_type, params)
                if ok:
                    return jsonify({"reply": f"✅ Đã thêm tag **{params.get('tag','')}** cho VM **{params.get('serverName','')}**", "fetchedAt": now, "actionDone": True})
                return jsonify({"reply": f"❌ Thêm tag thất bại: {data}", "fetchedAt": now})

            # ── VM creation guided flow ───────────────────────────────────────
            if action_type == "vm_create_guide":
                pre_name = params.get("vmName", "")

                # Flavors table (show top 15 sorted by vCPU then RAM)
                fl_rows = sorted(flavors, key=lambda x: (x.get("vcpus",0), x.get("ram",0)))[:15]
                fl_lines = "\n".join(
                    f"| `{f.get('id','?')}` | {f.get('name','?')} | {f.get('vcpus','?')} vCPU | {f.get('ram','?')} MB |"
                    for f in fl_rows)

                # Images grouped by OS type (max 12)
                img_rows = images[:12]
                img_lines = "\n".join(
                    f"| `{i.get('id','?')}` | {i.get('name','?')} | {i.get('imageType') or i.get('osType','?')} |"
                    for i in img_rows)

                # Networks + subnets
                net_sub = []
                for n in networks:
                    nid   = n.get("uuid") or n.get("id","?")
                    nname = n.get("name","?")
                    subs  = [s for s in subnets if s.get("networkId") == nid or s.get("networkUuid") == nid]
                    for s in subs:
                        net_sub.append(f"| `{s.get('id') or s.get('uuid','?')}` | {s.get('name','?')} | {nname} | {s.get('cidr','?')} |")
                    if not subs:
                        net_sub.append(f"| _(no subnet)_ | — | {nname} | — |")
                sub_lines = "\n".join(net_sub) or "| (không có subnet) |"

                # SSH keys
                key_lines = "\n".join(f"• `{k.get('id') or k.get('uuid','?')}` — {k.get('name','?')}" for k in sshkeys) or "_(chưa có SSH key nào)_"

                # Default volume type
                default_vt = vol_types[0] if vol_types else {}
                vt_id   = default_vt.get("id") or default_vt.get("uuid","?")
                vt_name = default_vt.get("name","SSD")

                name_hint = f"Tên VM đề xuất: **{pre_name}**\n\n" if pre_name else ""
                reply = f"""🖥️ **Tạo VM mới — Chọn thông số**

{name_hint}Hãy cho tôi biết (hoặc nói tự nhiên, tôi sẽ tự điền):

**1️⃣ Tên VM** — VD: `web-server-01`

**2️⃣ Flavor (cấu hình)**
| Flavor ID | Tên | vCPU | RAM |
|---|---|---|---|
{fl_lines}
_(Hỏi **liệt kê flavor** để xem đầy đủ)_

**3️⃣ Image (hệ điều hành)**
| Image ID | Tên | OS |
|---|---|---|
{img_lines}

**4️⃣ Subnet**
| Subnet ID | Tên | Network | CIDR |
|---|---|---|---|
{sub_lines}

**5️⃣ SSH Key** _(tuỳ chọn)_
{key_lines}

**6️⃣ Root disk** — Mặc định 40 GB, type: `{vt_name}` (`{vt_id}`)

---
💬 **Ví dụ:** _"Tạo VM tên web-02, Ubuntu 22.04, 4 vCPU 8GB RAM, subnet production, key deploy-key"_
Tôi sẽ tự map tên → ID và xin xác nhận trước khi tạo."""
                return jsonify({"reply": reply, "fetchedAt": now})

            # Extended actions (volume, FIP, SG, rename) → direct execute via action2
            # Actions requiring confirmation (medium risk)
            CONFIRM_ACTIONS = {"volume_attach","volume_detach","fip_associate","fip_disassociate","sg_attach","sg_detach","vm_rename","volume_rename","vm_snapshot","vm_create","vm_resize","vm_delete","volume_create","volume_delete","volume_resize","sshkey_create","sshkey_delete"}
            if action_type in CONFIRM_ACTIONS and params:
                # vm_create: show full spec in confirm message
                if action_type == "vm_create":
                    ssh_name = next((k.get("name","?") for k in sshkeys if k.get("id") == params.get("sshKeyId") or k.get("uuid") == params.get("sshKeyId")), params.get("sshKeyId") or "_(không có)_")
                    subnet_name = next((s.get("name","?") for s in subnets if s.get("id") == params.get("subnetId") or s.get("uuid") == params.get("subnetId")), params.get("subnetId","?"))
                    spec = (
                        f"🖥️ **Xác nhận tạo VM mới**\n\n"
                        f"| Thông số | Giá trị |\n|---|---|\n"
                        f"| Tên | **{params.get('name','?')}** |\n"
                        f"| Flavor | {params.get('flavorName') or params.get('flavorId','?')} |\n"
                        f"| OS / Image | {params.get('imageName') or params.get('imageId','?')} |\n"
                        f"| Subnet | {subnet_name} |\n"
                        f"| Root disk | {params.get('rootDiskSize',40)} GB |\n"
                        f"| SSH Key | {ssh_name} |\n"
                        f"| Floating IP | {'Có' if params.get('attachFloating') else 'Không'} |\n\n"
                        f"⚠️ VM sẽ được tạo và **tính phí ngay lập tức**. Xác nhận?"
                    )
                    return jsonify({
                        "reply": spec, "fetchedAt": now,
                        "needConfirm": True,
                        "pendingAction": {"type": action_type, "params": params, "desc": desc}
                    })
                # vm_resize: show cost comparison
                if action_type == "vm_resize":
                    new_flav_id  = params.get("flavorId","")
                    all_flavs    = ref_flavors()
                    new_flav_obj = next((f for f in all_flavs if f["id"] == new_flav_id), None)
                    # current VM flavor
                    cur_vm = next((v for v in vms if v.get("uuid") == params.get("serverId") or v.get("name") == params.get("serverName")), None)
                    cur_flav_name = (cur_vm or {}).get("flavor", {}).get("name") or (cur_vm or {}).get("flavorName", "?") if cur_vm else "?"
                    if new_flav_obj:
                        new_price = f"{new_flav_obj['price_vnd']:,}đ/tháng".replace(",",".")
                        spec = (
                            f"⬆️ **Xác nhận Resize VM**\n\n"
                            f"| | Hiện tại | Sau resize |\n|---|---|---|\n"
                            f"| VM | **{params.get('serverName','?')}** | **{params.get('serverName','?')}** |\n"
                            f"| Flavor | {cur_flav_name} | **{new_flav_obj['name']}** |\n"
                            f"| vCPU | — | {new_flav_obj['cpu']} vCPU |\n"
                            f"| RAM | — | {new_flav_obj['ram_gb']} GB |\n"
                            f"| Chi phí ước tính | — | ~{new_price} |\n\n"
                            f"⚠️ VM sẽ được **reboot** trong quá trình resize. Xác nhận?"
                        )
                        return jsonify({
                            "reply": spec, "fetchedAt": now,
                            "needConfirm": True,
                            "pendingAction": {"type": action_type, "params": params, "desc": desc}
                        })
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
        
        # Check if LLM returned structured action JSON
        action_data = None
        reply_work = reply.strip()

        # Try 1: Strip ```json ... ``` blocks and parse
        cleaned = re.sub(r'```(?:json)?\s*', '', reply_work).strip().rstrip('`').strip()
        try:
            d = json.loads(cleaned)
            if "__action__" in d:
                action_data = d
        except: pass
        
        # Try 2: Find JSON object anywhere in reply
        if not action_data:
            for m in re.finditer(r'\{[^{}]*"__action__"[^{}]*\}', reply_work, re.DOTALL):
                try:
                    d = json.loads(m.group())
                    if "__action__" in d:
                        action_data = d
                        break
                except: pass
        
        # Try 3: Find JSON in code block content
        if not action_data:
            for m in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', reply_work, re.DOTALL):
                try:
                    d = json.loads(m.group(1))
                    if "__action__" in d:
                        action_data = d
                        break
                except: pass
        
        if action_data:
            action_type = action_data.get("__action__")
            params      = action_data.get("params", {})
            desc        = action_data.get("desc", f"Thực hiện {action_type}")
            
            # For volume actions: lookup real UUID from name if needed
            if action_type in ("volume_attach", "volume_detach"):
                vol_id = params.get("volumeId", "")
                if vol_id and not vol_id.startswith("vol-"):
                    # LLM gave name, not UUID — lookup from volumes list
                    for v in volumes:
                        if v.get("name","").lower() == vol_id.lower():
                            params["volumeId"] = v.get("uuid", vol_id)
                            params["volumeName"] = v.get("name", vol_id)
                            vol_type = v.get("volumeType") or {}
                            params["zoneId"] = vol_type.get("zoneId") or "0745BE12-9433-4DD4-90A1-384631504EBE"
                            break
                # Lookup server UUID from name if needed
                srv_id = params.get("serverId", "")
                if srv_id and not srv_id.startswith("ins-"):
                    for v in vms:
                        if v.get("name","").lower() == params.get("serverName","").lower():
                            params["serverId"] = v.get("uuid", srv_id)
                            break

            confirm_reply = f"⚠️ **Xác nhận hành động**\n\n{desc}\n\nBạn có chắc muốn thực hiện không? Nhấn nút bên dưới hoặc gõ **xác nhận**."
            return jsonify({
                "reply": confirm_reply, "fetchedAt": now,
                "needConfirm": True,
                "pendingAction": {"type": action_type, "params": params, "desc": desc}
            })
        
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





# ── Scheduled job runner ──────────────────────────────────────────────────────
def run_scheduled_job(job_id: str):
    """Execute a scheduled VM action."""
    job = _scheduled_jobs.get(job_id)
    if not job:
        return
    customer    = job.get("customer", "unknown")
    action_type = job["action"]
    params      = job["params"]
    server_name = params.get("serverName", "VM")
    action_label = {"vm_start": "Khởi động", "vm_stop": "Tắt", "vm_reboot": "Reboot"}.get(action_type, action_type)
    try:
        creds  = job["creds"]
        token, user_info = fetch_gn_token(creds["clientId"], creds["clientSecret"])
        uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
        ok, err, vm_after = execute_vm_action(token, uid, creds["projectId"], action_type, params)
        status = vm_after.get("status", "?") if vm_after else "unknown"
        result_msg = f"Thành công — trạng thái: {status}" if ok else f"Thất bại: {err}"
        print(f"[SCHEDULE] Job {job_id}: {action_type} on {server_name} → {status}")
        # Persist result
        db_update_schedule_status(job_id, 'done' if ok else 'failed', result_msg)
        # Audit log
        db_write_audit(customer, creds["projectId"], action_type, server_name, params,
                       'success' if ok else 'failed', result_msg, performed_by="scheduler")
        # Notification
        icon = "✅" if ok else "❌"
        db_write_notification(customer,
            f"{icon} Lịch hẹn: {action_label} VM",
            f"{action_label} VM **{server_name}** — {result_msg}",
            ntype="success" if ok else "error")
    except Exception as e:
        print(f"[SCHEDULE] Job {job_id} error: {e}")
        db_update_schedule_status(job_id, 'failed', str(e))
        db_write_notification(customer,
            f"❌ Lịch hẹn thất bại: {action_label} VM",
            f"Lỗi khi {action_label.lower()} VM **{server_name}**: {e}",
            ntype="error")
    finally:
        _scheduled_jobs.pop(job_id, None)


def _do_schedule(client_id, client_secret, project_id, action, params, run_at_str, tz_str="Asia/Ho_Chi_Minh", customer=""):
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

        action_label = {"vm_start": "khởi động", "vm_stop": "tắt", "vm_reboot": "reboot"}.get(action, action)
        server_name  = params.get("serverName", "VM")
        desc = f"{action_label} VM {server_name} lúc {run_time.strftime('%H:%M %d/%m/%Y')}"
        job_id = f"{action}_{params.get('serverId','')[:8]}_{run_time.strftime('%Y%m%d%H%M')}"
        creds  = {"clientId": client_id, "clientSecret": client_secret, "projectId": project_id}

        _scheduled_jobs[job_id] = {
            "desc":     desc,
            "action":   action,
            "params":   params,
            "creds":    creds,
            "run_time": run_time.isoformat(),
            "customer": customer,
        }
        scheduler.add_job(
            run_scheduled_job, trigger="date", run_date=run_time,
            args=[job_id], id=job_id, replace_existing=True,
        )
        # Persist to DB
        db_write_schedule(job_id, customer, project_id, action, params, creds, run_time.isoformat(), desc)
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
    action_type   = body.get("action", "")
    params        = body.get("params", {})
    run_at_str    = body.get("runAt", "")
    tz_str        = body.get("timezone", "Asia/Ho_Chi_Minh")

    if not all([client_id, project_id, action_type, params, run_at_str]):
        return jsonify({"error": "Thiếu thông tin: clientId, projectId, action, params, runAt"}), 400

    result = _do_schedule(client_id, client_secret, project_id, action_type, params, run_at_str, tz_str)
    if result["ok"]:
        return jsonify(result)
    return jsonify({"error": result.get("error", "Lỗi đặt lịch")}), 400

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
    """Execute non-VM actions using exact endpoints from VNG Cloud OpenAPI spec."""
    P  = project_id
    OK = (200, 201, 202, 204)

    # ── Volume attach ────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/volumes/{volumeId}/servers/{serverId}/attach
    # Body: {persistentVolume: bool, tags: [], zoneId: str}
    if action_type == "volume_attach":
        volume_id = params.get("volumeId")
        server_id = params.get("serverId", "")
        print(f"[ATTACH] vol={volume_id} srv={server_id}")
        # AttachVolumeRequest body is empty per Terraform provider spec
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/volumes/{volume_id}/servers/{server_id}/attach", {})
        print(f"[ATTACH] -> {s} {str(d)[:200]}")
        return s in OK, d

    # ── Volume detach ────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/volumes/{volumeId}/servers/{serverId}/detach
    # Body: {persistentVolume: bool, tags: []}
    if action_type == "volume_detach":
        volume_id   = params.get("volumeId")
        volume_name = params.get("volumeName", "")
        server_id   = params.get("serverId", "")
        if server_id and not server_id.startswith("ins-"):
            server_id = "ins-" + server_id  # keep ins- prefix like attach
        # Block detach of boot volume
        if "boot" in volume_name.lower():
            return False, {"message": "Không thể gỡ boot volume — đây là ổ đĩa hệ thống của VM"}
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/volumes/{volume_id}/servers/{server_id}/detach", {})
        print(f"[DETACH] vol={volume_id} srv={server_id} -> {s} {str(d)[:150]}")
        return s in OK, d

    # ── FIP associate ────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/wan-ips/{wanIpId}/attach
    # Body: {networkInterfaceId: str, tags: []}
    if action_type == "fip_associate":
        server_id    = params.get("serverId")
        wan_ip_id    = params.get("wanIpId")
        interface_id = params.get("networkInterfaceId", "")
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/wan-ips/{wan_ip_id}/attach",
            {"networkInterfaceId": interface_id, "tags": []})
        return s in OK, d

    # ── FIP disassociate ─────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/wan-ips/{wanIpId}/detach
    # Body: {networkInterfaceId: str, tags: []}
    if action_type == "fip_disassociate":
        server_id    = params.get("serverId")
        wan_ip_id    = params.get("wanIpId")
        interface_id = params.get("networkInterfaceId", "")
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/wan-ips/{wan_ip_id}/detach",
            {"networkInterfaceId": interface_id, "tags": []})
        return s in OK, d

    # ── Update SecGroups ─────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/update-sec-group
    # Body: {serverId: str, securityGroup: [str]}
    if action_type in ("sg_attach", "sg_detach"):
        server_id = params.get("serverId")
        sg_ids    = params.get("sgIds", [])
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/update-sec-group",
            {"serverId": server_id, "securityGroup": sg_ids})
        return s in OK, d

    # ── SG rule add ──────────────────────────────────────────────────────────
    # POST /v2/{projectId}/secgroups/{secgroupId}/secgroupRules
    if action_type == "sg_rule_add":
        sg_id = params.get("sgId")
        rule  = params.get("rule", {})
        s, d = gn_api(token, uid, "POST",
            f"v2/{P}/secgroups/{sg_id}/secgroupRules", rule)
        return s in OK, d

    # ── SG rule remove ───────────────────────────────────────────────────────
    # DELETE /v2/{projectId}/secgroups/{secgroupId}/secgroupRules/{ruleId}
    if action_type == "sg_rule_remove":
        sg_id   = params.get("sgId")
        rule_id = params.get("ruleId")
        s, d = gn_api(token, uid, "DELETE",
            f"v2/{P}/secgroups/{sg_id}/secgroupRules/{rule_id}")
        return s in OK, d

    # ── Rename VM ────────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/rename
    # Body: {newName: str, tags: []}
    if action_type == "vm_rename":
        server_id = params.get("serverId")
        new_name  = params.get("newName")
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/rename",
            {"newName": new_name, "tags": []})
        return s in OK, d

    # ── Rename Volume ────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/volumes/{volumeId}/rename
    # Body: {newName: str, tags: []}
    if action_type == "volume_rename":
        volume_id = params.get("volumeId")
        new_name  = params.get("newName")
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/volumes/{volume_id}/rename",
            {"newName": new_name, "tags": []})
        return s in OK, d

    # ── Create Snapshot ──────────────────────────────────────────────────────
    # POST /v2/{projectId}/servers/{serverId}/snapshots
    # Body: {name: str, description: str, isPermanently: bool, retainedDays: int}
    if action_type == "vm_snapshot":
        server_id = params.get("serverId")
        snap_name = params.get("snapshotName", f"snapshot-{server_id[:8]}")
        s, d = gn_api(token, uid, "POST",
            f"v2/{P}/servers/{server_id}/snapshots",
            {"name": snap_name, "description": snap_name,
             "isPermanently": False, "retainedDays": 7})
        return s in OK, d

    # ── Create VM ────────────────────────────────────────────────────────────
    # POST /v2/{projectId}/servers
    # Required: name, flavorId, imageId, networkId, subnetId, rootDiskSize, rootDiskTypeId, encryptionVolume
    if action_type == "vm_create":
        network_id = params.get("networkId") or ""
        subnet_id  = params.get("subnetId")  or ""

        def _parse_list(d):
            """Extract list from any API response shape."""
            if isinstance(d, list):   return d
            if isinstance(d, dict):
                for k in ("listData","data","subnets","networks","items","results"):
                    if d.get(k): return d[k]
            return []

        # ── Fetch subnets — try multiple strategies ──────────────────────────────
        _subnets  = []
        _networks = []

        # Strategy 1: GET /subnets (may be 403 if IAM doesn't allow it)
        sn_s, sn_d = gn_api(token, uid, "GET", f"v2/{P}/subnets")
        _subnets = _parse_list(sn_d) if sn_s == 200 else []
        print(f"[VM_CREATE] /subnets -> status={sn_s} count={len(_subnets)} raw={str(sn_d)[:200]}")

        # Strategy 2: GET /networks — extract embedded subnets or call per-network endpoint
        nw_s, nw_d = gn_api(token, uid, "GET", f"v2/{P}/networks")
        _networks = _parse_list(nw_d) if nw_s == 200 else []
        print(f"[VM_CREATE] /networks -> status={nw_s} count={len(_networks)} raw={str(nw_d)[:300]}")

        if not _subnets and _networks:
            for _net in _networks:
                _nid = _net.get("uuid") or _net.get("id") or _net.get("networkId") or ""
                _net_name = _net.get("name","?")
                print(f"[VM_CREATE] network object keys: {list(_net.keys())} nid={_nid}")

                # 2a: subnet embedded in network object
                _embedded = (_net.get("subnets") or _net.get("subnetList") or
                             _net.get("subnetObjects") or [])
                if isinstance(_embedded, list) and _embedded:
                    print(f"[VM_CREATE] embedded subnets in network {_net_name}: {_embedded[:1]}")
                    _subnets = _embedded
                    # Attach networkId if missing
                    for _s in _subnets:
                        if not (_s.get("networkId") or _s.get("networkUuid")):
                            _s["networkId"] = _nid
                    break

                if not _nid:
                    continue

                # 2b: GET /networks/{id}/subnets
                ns_s, ns_d = gn_api(token, uid, "GET", f"v2/{P}/networks/{_nid}/subnets")
                _nsubs = _parse_list(ns_d)
                print(f"[VM_CREATE] /networks/{_nid}/subnets -> status={ns_s} count={len(_nsubs)} raw={str(ns_d)[:300]}")
                if _nsubs:
                    _subnets = _nsubs
                    for _s in _subnets:
                        if not (_s.get("networkId") or _s.get("networkUuid")):
                            _s["networkId"] = _nid
                    break

        _subnet_zone = ""   # zone name detected from subnet, passed as zoneId to API

        if _subnets:
            _sn = _subnets[0]
            print(f"[VM_CREATE] using subnet object: {_sn}")
            # Extract subnet ID — try every possible field name
            subnet_id  = (subnet_id  or
                          _sn.get("uuid") or _sn.get("id") or
                          _sn.get("subnetId") or _sn.get("subnetUuid") or
                          _sn.get("subnetID") or "")
            # Extract network ID — from subnet object or from first network
            network_id = (network_id or
                          _sn.get("networkUuid") or _sn.get("networkId") or
                          _sn.get("vpcId") or _sn.get("vpcUuid") or
                          (_networks[0].get("uuid") or _networks[0].get("id") if _networks else ""))

            # Detect zone from subnet and re-resolve flavor/image/voltype IDs for that zone
            _zone_obj  = _sn.get("zone") or {}
            _zone_name = (_zone_obj.get("name") if isinstance(_zone_obj, dict) else str(_zone_obj)) or ""
            print(f"[VM_CREATE] raw zone from subnet: {repr(_zone_name)}")

            # Normalize short zone name to full name: "HCM-1B" → "HCM03-1B"
            def _normalize_zone(zn):
                if not zn or "-" not in zn:
                    return zn
                _rgn = zn[:3]  # "HCM" or "HAN"
                _suffix = zn.split("-")[-1]  # "1B", "1A", "1C"
                # Scan reference dirs for matching zone
                _ref_region_dir = os.path.join(_REF_DIR, _rgn)
                if os.path.isdir(_ref_region_dir):
                    for _d in sorted(os.listdir(_ref_region_dir)):
                        if _d.endswith("-" + _suffix) and os.path.isdir(os.path.join(_ref_region_dir, _d)):
                            return _d  # e.g. "HCM03-1B"
                return zn  # fallback to original

            # zone_name like "HCM03-1B" or "HCM-1B" → normalize to "HCM03-1B"
            if _zone_name and "-" in _zone_name:
                _zone_name = _normalize_zone(_zone_name)
                _region = _zone_name[:3]   # e.g. "HCM"
                _zone   = _zone_name       # e.g. "HCM03-1B"
                _subnet_zone = _zone_name  # pass as zoneId in POST body
                print(f"[VM_CREATE] subnet zone detected: region={_region} zone={_zone}")
                # Re-resolve IDs from correct zone's reference data using stored names
                _flavor_name = params.get("flavorName", "")
                _image_name  = params.get("imageName", "")
                for _f in ref_flavors(_region, _zone):
                    if _f["name"] == _flavor_name:
                        params["flavorId"] = _f["id"]
                        print(f"[VM_CREATE] re-resolved flavorId={_f['id']} for zone {_zone}")
                        break
                for _i in ref_images(_region, _zone):
                    if _i["name"] == _image_name:
                        params["imageId"] = _i["id"]
                        print(f"[VM_CREATE] re-resolved imageId={_i['id']} for zone {_zone}")
                        break
                for _v in ref_vol_types(_region, _zone):
                    if _v.get("default"):
                        params["rootDiskTypeId"] = _v["id"]
                        print(f"[VM_CREATE] re-resolved rootDiskTypeId={_v['id']} for zone {_zone}")
                        break
        elif _networks and not subnet_id:
            # Last resort: use first network ID, hope API accepts without subnetId
            _net0 = _networks[0]
            network_id = network_id or _net0.get("uuid") or _net0.get("id") or ""
            print(f"[VM_CREATE] WARNING: no subnet found, using network only networkId={network_id}")

        if not network_id or not subnet_id:
            _sn_debug = str(_subnets[0]) if _subnets else "none"
            return False, {"message": (
                f"Không lấy được network/subnet. "
                f"networkId={repr(network_id)} subnetId={repr(subnet_id)}. "
                f"networks={len(_networks)} subnets={len(_subnets)}. "
                f"subnet_obj={_sn_debug[:300]}"
            )}

        def _build_server_body():
            body = {
                "name":            params.get("name"),
                "flavorId":        params.get("flavorId"),
                "imageId":         params.get("imageId"),
                "networkId":       network_id,
                "subnetId":        subnet_id,
                "rootDiskSize":    params.get("rootDiskSize", 40),
                "rootDiskTypeId":  params.get("rootDiskTypeId"),
                "encryptionVolume": False,
                "attachFloating":  params.get("attachFloating", False),
                "sshKeyId":        params.get("sshKeyId") or None,
                "securityGroup":   params.get("secgroupIds") or params.get("securityGroup") or [],
                "tags":            [],
            }
            # Include zoneId if detected from subnet
            if _subnet_zone:
                body["zoneId"] = _subnet_zone
            return body

        print(f"[VM_CREATE] POST /servers flavorId={params.get('flavorId')} imageId={params.get('imageId')}")
        s, d = gn_api(token, uid, "POST", f"v2/{P}/servers", _build_server_body())
        print(f"[VM_CREATE] POST /servers -> {s} {str(d)[:300]}")

        # Auto-retry if zone mismatch: parse error to get correct zone and re-resolve
        if s not in OK:
            err_msg = ""
            if isinstance(d, dict):
                err_msg = d.get("message", "") or str(d)
            elif isinstance(d, list) and d:
                err_msg = str(d[0].get("message", d[0]))

            # Pattern: "isn't allowed using at zone id HCM03-1B"
            _zone_match = re.search(r"zone id (HCM\d+-\d+[A-Z]|HAN\d+-\d+[A-Z])", err_msg)
            if _zone_match:
                _correct_zone = _zone_match.group(1)   # e.g. "HCM03-1B"
                _correct_region = _correct_zone[:3]     # e.g. "HCM"
                print(f"[VM_CREATE] Zone mismatch detected — retrying with zone {_correct_zone}")
                _flavor_name = params.get("flavorName", "")
                _image_name  = params.get("imageName", "")
                for _f in ref_flavors(_correct_region, _correct_zone):
                    if _f["name"] == _flavor_name:
                        params["flavorId"] = _f["id"]
                        print(f"[VM_CREATE] retry flavorId={_f['id']}")
                        break
                for _i in ref_images(_correct_region, _correct_zone):
                    if _i["name"] == _image_name:
                        params["imageId"] = _i["id"]
                        print(f"[VM_CREATE] retry imageId={_i['id']}")
                        break
                for _v in ref_vol_types(_correct_region, _correct_zone):
                    if _v.get("default"):
                        params["rootDiskTypeId"] = _v["id"]
                        print(f"[VM_CREATE] retry rootDiskTypeId={_v['id']}")
                        break
                s, d = gn_api(token, uid, "POST", f"v2/{P}/servers", _build_server_body())
                print(f"[VM_CREATE] RETRY POST /servers -> {s} {str(d)[:300]}")

        return s in OK, d

    # ── Resize VM ─────────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/resize
    # Required: flavorId, serverId
    if action_type == "vm_resize":
        server_id = params.get("serverId")
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/resize",
            {"flavorId": params.get("flavorId"), "serverId": server_id})
        return s in OK, d

    # ── Delete VM ─────────────────────────────────────────────────────────────
    # DELETE /v2/{projectId}/servers/{serverId}
    if action_type == "vm_delete":
        server_id = params.get("serverId")
        s, d = gn_api(token, uid, "DELETE", f"v2/{P}/servers/{server_id}")
        return s in OK, d

    # ── Create Volume ─────────────────────────────────────────────────────────
    # POST /v2/{projectId}/volumes
    if action_type == "volume_create":
        s, d = gn_api(token, uid, "POST", f"v2/{P}/volumes", {
            "name":         params.get("name"),
            "size":         params.get("size", 20),
            "volumeTypeId": params.get("volumeTypeId", "vtype-2fc64a6c-38e3-4f08-93a5-18018cb3ab23"),
            "tags":         [],
        })
        return s in OK, d

    # ── Delete Volume ─────────────────────────────────────────────────────────
    # DELETE /v2/{projectId}/volumes/{volumeId}
    if action_type == "volume_delete":
        volume_id = params.get("volumeId")
        s, d = gn_api(token, uid, "DELETE", f"v2/{P}/volumes/{volume_id}")
        return s in OK, d

    # ── Resize Volume ─────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/volumes/{volumeId}
    if action_type == "volume_resize":
        volume_id = params.get("volumeId")
        new_size  = params.get("size")
        if not volume_id or not new_size:
            return False, {"message": "Thiếu volumeId hoặc size"}
        vol_id_full = volume_id if str(volume_id).startswith("vol-") else f"vol-{volume_id}"
        s, d = gn_api(token, uid, "PUT", f"v2/{P}/volumes/{vol_id_full}", {"size": int(new_size)})
        print(f"[VOLUME_RESIZE] vol={vol_id_full} size={new_size} -> {s} {str(d)[:200]}")
        return s in OK, d

    # ── SSH Key: Create ───────────────────────────────────────────────────────
    # POST /v2/{projectId}/sshkeys
    if action_type == "sshkey_create":
        key_name = params.get("name")
        if not key_name:
            return False, {"message": "Thiếu tên SSH Key"}
        s, d = gn_api(token, uid, "POST", f"v2/{P}/sshkeys", {"name": key_name})
        print(f"[SSHKEY_CREATE] name={key_name} -> {s} {str(d)[:300]}")
        return s in OK, d

    # ── SSH Key: Delete ───────────────────────────────────────────────────────
    # DELETE /v2/{projectId}/sshkeys/{keypairId}
    if action_type == "sshkey_delete":
        key_id = params.get("keyId")
        if not key_id:
            return False, {"message": "Thiếu keyId"}
        s, d = gn_api(token, uid, "DELETE", f"v2/{P}/sshkeys/{key_id}")
        print(f"[SSHKEY_DELETE] keyId={key_id} -> {s} {str(d)[:200]}")
        return s in OK, d

    # ── Tag resource ──────────────────────────────────────────────────────────
    # PUT /v2/{projectId}/servers/{serverId}/tags
    # Body: {"tags": [{"key": "env", "value": "prod"}]}
    if action_type == "resource_tag":
        server_id = params.get("serverId")
        tag_raw   = params.get("tag", "")          # format: "key:value" or "key=value" or plain
        if ":" in tag_raw:
            k, v = tag_raw.split(":", 1)
        elif "=" in tag_raw:
            k, v = tag_raw.split("=", 1)
        else:
            k, v = tag_raw, tag_raw
        s, d = gn_api(token, uid, "PUT",
            f"v2/{P}/servers/{server_id}/tags",
            {"tags": [{"key": k.strip(), "value": v.strip()}]})
        return s in OK, d

    return False, {"error": f"Unknown action: {action_type}"}



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

# ── Audit Log API ─────────────────────────────────────────────────────────────
@app.route("/api/audit", methods=["GET"])
@admin_required
def get_audit_log():
    customer = request.args.get("customer", "")
    limit    = int(request.args.get("limit", 100))
    logs     = db_get_audit(customer or None, limit)
    return jsonify({"logs": logs})

# ── Notifications API ─────────────────────────────────────────────────────────
@app.route("/api/notifications", methods=["GET"])
@admin_required
def get_notifications():
    customer     = request.args.get("customer", "")
    unread_only  = request.args.get("unread", "false").lower() == "true"
    if not customer:
        return jsonify({"notifications": [], "unread": 0})
    notifs = db_get_notifications(customer, unread_only)
    unread = sum(1 for n in db_get_notifications(customer) if not n.get("read"))
    return jsonify({"notifications": notifs, "unread": unread})

@app.route("/api/notifications/read", methods=["POST"])
@admin_required
def mark_notifications_read():
    body     = request.get_json() or {}
    customer = body.get("customer", "")
    if customer:
        db_mark_notifications_read(customer)
    return jsonify({"ok": True})

# ── Schedule management API ────────────────────────────────────────────────────
@app.route("/api/schedules", methods=["GET"])
@admin_required
def list_schedules_v2():
    """List all scheduled jobs from DB (includes history)."""
    customer = request.args.get("customer", "")
    conn = get_conn(); cur = conn.cursor()
    if customer:
        cur.execute(f"SELECT * FROM scheduled_jobs WHERE customer={_PH} ORDER BY run_time DESC LIMIT 100", (customer,))
    else:
        cur.execute("SELECT * FROM scheduled_jobs ORDER BY run_time DESC LIMIT 200")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    # Add in-memory pending flag
    for r in rows:
        r['in_memory'] = r['job_id'] in _scheduled_jobs
    return jsonify({"schedules": rows})

@app.route("/api/schedules/<job_id>", methods=["DELETE"])
@admin_required
def cancel_schedule_v2(job_id):
    if job_id in _scheduled_jobs:
        try: scheduler.remove_job(job_id)
        except: pass
        _scheduled_jobs.pop(job_id, None)
    db_update_schedule_status(job_id, 'cancelled', 'Cancelled by admin')
    return jsonify({"ok": True, "message": "Đã hủy lịch hẹn"})


# ── vMonitor Integration ──────────────────────────────────────────────────────
VMONITOR_BASE = "https://vmonitor.console.vngcloud.vn/vmonitor-api/api/v1"
VMONITOR_METRICS = {
    "cpu":      "vserver.cpu.utilization_norm_perc",
    "net_in":   "vserver.net.in_bytes_sec",
    "net_out":  "vserver.net.out_bytes_sec",
    "disk_read":  "vserver.disk.read_bytes_sec",
    "disk_write": "vserver.disk.write_bytes_sec",
    "mem":      "vserver.mem.used_percent",
}

def _vmonitor_query(token, vm_id, metric_key, minutes=60, period=60):
    """Query a single metric for a VM from vMonitor. Returns list of [ts, val]."""
    import time
    now_ms  = int(time.time() * 1000)
    start_ms = now_ms - minutes * 60 * 1000
    metric_name = VMONITOR_METRICS.get(metric_key, metric_key)
    body = {
        "type": "SIMPLE",
        "data": {
            "graph": {
                "name": metric_name,
                "dimensions": f"resource_id:{vm_id},product:vserver",
                "statistics": "avg",
                "group_by": "none",
                "offset": 0,
                "limit": "",
                "rollup": "",
                "rate": 0,
            },
            "start_time": start_ms,
            "end_time":   now_ms,
            "period":     period,
            "alarm": False,
            "reduction": None,
        }
    }
    r = requests.post(
        f"{VMONITOR_BASE}/statistics",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, verify=False, timeout=15,
    )
    if not r.ok:
        return []
    data = r.json()
    if isinstance(data, list) and data:
        return data[0].get("statistics", [])
    return []

def _vmonitor_latest(token, vm_id, metric_key):
    """Return the latest non-null value for a metric."""
    pts = _vmonitor_query(token, vm_id, metric_key, minutes=15, period=60)
    for ts, val in reversed(pts):
        if val is not None and val != "null":
            try: return float(val)
            except: pass
    return None

@app.route("/api/vmonitor/metrics/<vm_id>", methods=["GET"])
@admin_required
def vmonitor_vm_metrics(vm_id):
    """
    Query vMonitor metrics for a specific VM.
    Query params:
      - customer: customer name (required to get credentials)
      - metric: cpu|net_in|net_out|disk_read|disk_write|mem (default: cpu)
      - minutes: time range in minutes (default: 60)
      - period: aggregation period in seconds (default: 60)
    Returns: {metric, vm_id, datapoints: [[ts_sec, value], ...], latest: float}
    """
    customer_name = request.args.get("customer", "")
    metric_key    = request.args.get("metric", "cpu")
    minutes       = int(request.args.get("minutes", 60))
    period        = int(request.args.get("period", 60))

    conn = get_conn(); cur = conn.cursor()
    if customer_name:
        cur.execute(f"SELECT client_id, client_secret FROM customers WHERE name={_PH}", (customer_name,))
    else:
        cur.execute("SELECT client_id, client_secret FROM customers LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Customer not found"}), 404

    client_id, client_secret = row
    try:
        token, _ = fetch_gn_token(client_id, client_secret)
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 500

    pts = _vmonitor_query(token, vm_id, metric_key, minutes=minutes, period=period)
    latest = None
    for ts, val in reversed(pts):
        if val is not None and val != "null":
            try: latest = float(val); break
            except: pass

    return jsonify({
        "metric":     metric_key,
        "metric_name": VMONITOR_METRICS.get(metric_key, metric_key),
        "vm_id":      vm_id,
        "minutes":    minutes,
        "period":     period,
        "datapoints": pts,
        "latest":     latest,
    })


@app.route("/api/vmonitor/overview", methods=["GET"])
@admin_required
def vmonitor_overview():
    """
    Get latest CPU, memory for all VMs of a customer.
    Query params:
      - customer: customer name
    Returns: [{vm_id, cpu, net_in, net_out, disk_read, disk_write}, ...]
    """
    customer_name = request.args.get("customer", "")
    conn = get_conn(); cur = conn.cursor()
    if customer_name:
        cur.execute(f"SELECT client_id, client_secret, project_id FROM customers WHERE name={_PH}", (customer_name,))
    else:
        cur.execute("SELECT client_id, client_secret, project_id FROM customers LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Customer not found"}), 404

    client_id, client_secret, project_id = row
    try:
        token, user_info = fetch_gn_token(client_id, client_secret)
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 500

    uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
    sv, vms_data = gn_api(token, uid, "GET", f"v2/{project_id}/servers")
    vms = _parse_list(vms_data) if sv == 200 else []

    result = []
    for vm in vms[:10]:  # limit to avoid rate limit
        vm_id = vm.get("id", "")
        if not vm_id:
            continue
        cpu = _vmonitor_latest(token, vm_id, "cpu")
        net_in  = _vmonitor_latest(token, vm_id, "net_in")
        net_out = _vmonitor_latest(token, vm_id, "net_out")
        result.append({
            "vm_id":   vm_id,
            "name":    vm.get("name", vm_id),
            "status":  vm.get("status", ""),
            "cpu":     cpu,
            "net_in":  net_in,
            "net_out": net_out,
        })

    return jsonify({"vms": result, "customer": customer_name})


# ── Health Alert (background check) ──────────────────────────────────────────
def _run_health_alerts():
    """Check all customers for SHUTOFF/ERROR VMs and create notifications."""
    customers = get_all_customers()
    for cust in customers:
        try:
            token, user_info = fetch_gn_token(cust["client_id"], cust["client_secret"])
            uid = str(user_info.get("accountId") or user_info.get("userId", "0"))
            P   = cust["project_id"]
            sv, dv = gn_api(token, uid, "GET", f"v2/{P}/servers")
            vms = _parse_list(dv) if sv == 200 else []
            shutoff = [v for v in vms if v.get("status") in ("SHUTOFF", "ERROR")]
            if shutoff:
                names = ", ".join(v.get("name","?") for v in shutoff[:5])
                db_write_notification(cust["name"],
                    f"⚠️ {len(shutoff)} VM không hoạt động",
                    f"Các VM đang SHUTOFF/ERROR: {names}",
                    ntype="warning")
        except Exception as e:
            print(f"[HEALTH_ALERT] {cust['name']}: {e}")

# Schedule health alert every 30 minutes
scheduler.add_job(_run_health_alerts, trigger="interval", minutes=30,
                  id="health_alerts", replace_existing=True)

# ── Serve static chatbot UI ───────────────────────────────────────────────────
@app.route("/")
@admin_required
def index():
    return send_from_directory("static", "index.html")


@app.route("/dashboard")
@admin_required
def dashboard_page():
    return send_from_directory("static", "dashboard.html")


@app.route("/api/dashboard/stats", methods=["GET"])
@admin_required
def dashboard_stats():
    """Aggregate stats for the monitoring dashboard."""
    conn = get_conn(); cur = conn.cursor()

    # ── Audit stats: last 7 days by day ──────────────────────────────────────
    cur.execute("""
        SELECT date(created_at) as day, status, COUNT(*) as cnt
        FROM audit_log
        WHERE created_at >= date('now', '-6 days')
        GROUP BY day, status
        ORDER BY day
    """)
    audit_rows = cur.fetchall()

    days_map = {}
    for row in audit_rows:
        day, status, cnt = row[0], row[1], row[2]
        if day not in days_map:
            days_map[day] = {"success": 0, "failed": 0}
        if status == "success":
            days_map[day]["success"] += cnt
        else:
            days_map[day]["failed"] += cnt

    # Fill missing days
    from datetime import date, timedelta
    today = date.today()
    audit_by_day = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        v = days_map.get(d, {"success": 0, "failed": 0})
        audit_by_day.append({"day": d, **v})

    # ── Top actions ───────────────────────────────────────────────────────────
    cur.execute("""
        SELECT action, COUNT(*) as cnt FROM audit_log
        GROUP BY action ORDER BY cnt DESC LIMIT 8
    """)
    top_actions = [{"action": r[0], "count": r[1]} for r in cur.fetchall()]

    # ── Total counts ─────────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM audit_log")
    total_actions = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM audit_log WHERE status='success'")
    total_success = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM audit_log WHERE status='failed'")
    total_failed = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM audit_log WHERE date(created_at)=date('now')")
    today_actions = cur.fetchone()[0]

    # ── Schedules overview ────────────────────────────────────────────────────
    cur.execute("SELECT status, COUNT(*) FROM scheduled_jobs GROUP BY status")
    sched_rows = cur.fetchall()
    sched_stats = {r[0]: r[1] for r in sched_rows}

    cur.execute("""
        SELECT customer, action, run_time FROM scheduled_jobs
        WHERE status='pending' ORDER BY run_time ASC LIMIT 5
    """)
    upcoming = [{"customer": r[0], "action": r[1], "run_time": r[2]} for r in cur.fetchall()]

    # ── Customers count ───────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM customers")
    total_customers = cur.fetchone()[0]

    # ── Per-customer action count ─────────────────────────────────────────────
    cur.execute("""
        SELECT customer, COUNT(*) as cnt FROM audit_log
        GROUP BY customer ORDER BY cnt DESC LIMIT 10
    """)
    per_customer = [{"customer": r[0], "count": r[1]} for r in cur.fetchall()]

    # ── Recent audit entries ──────────────────────────────────────────────────
    cur.execute("""
        SELECT customer, action, resource, status, message, created_at
        FROM audit_log ORDER BY created_at DESC LIMIT 10
    """)
    recent = [
        {"customer": r[0], "action": r[1], "resource": r[2],
         "status": r[3], "message": r[4], "created_at": r[5]}
        for r in cur.fetchall()
    ]

    conn.close()

    return jsonify({
        "totals": {
            "actions": total_actions,
            "success": total_success,
            "failed": total_failed,
            "today": today_actions,
            "customers": total_customers,
        },
        "audit_by_day": audit_by_day,
        "top_actions": top_actions,
        "schedule_stats": sched_stats,
        "upcoming_schedules": upcoming,
        "per_customer": per_customer,
        "recent_audit": recent,
    })



@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
