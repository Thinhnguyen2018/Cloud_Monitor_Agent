"""Standalone monitoring process — calls internal logic directly via shared DB."""
import time, os, sys

sys.path.insert(0, os.path.dirname(__file__))

# Wait for gunicorn to start and stabilize first
time.sleep(15)
print("[MONITOR] Starting after 15s delay")

# Import ONLY what we need — avoid triggering Flask/scheduler/token-cache init
import os as _os
DATABASE_URL = _os.environ.get("DATABASE_URL", "")

def get_conn():
    if DATABASE_URL:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    import sqlite3
    return sqlite3.connect("agent.db")

def get_all_customers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id,name,client_id,client_secret,project_id FROM customers ORDER BY name")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows

def db_write_notification(customer, title, body, ntype="info"):
    try:
        conn = get_conn()
        cur = conn.cursor()
        ph = "%s" if DATABASE_URL else "?"
        time_expr = "NOW() - INTERVAL '5 minutes'" if DATABASE_URL else "datetime('now', '-5 minutes')"
        resolved_false = "false" if DATABASE_URL else "0"
        cur.execute(f"SELECT id FROM notifications WHERE customer={ph} AND title={ph} AND resolved={resolved_false} AND created_at >= {time_expr} LIMIT 1", (customer, title))
        if cur.fetchone():
            conn.close(); return
        cur.execute(f"INSERT INTO notifications (customer,title,body,type) VALUES ({ph},{ph},{ph},{ph})", (customer, title, body, ntype))
        conn.commit(); conn.close()
        print(f"[MONITOR] Notification written: [{ntype}] {title}")
    except Exception as e:
        print(f"[MONITOR] DB write error: {e}")

def fetch_token(client_id, client_secret):
    import requests, base64
    PROXY_TOKEN_URL = _os.environ.get("PROXY_TOKEN_URL", "")
    ADMIN_PASSWORD  = _os.environ.get("ADMIN_PASSWORD", "admin12345")
    if PROXY_TOKEN_URL:
        # Call app proxy: expects JSON body + X-Proxy-Secret header
        r = requests.post(PROXY_TOKEN_URL,
                          headers={"Content-Type": "application/json",
                                   "X-Proxy-Secret": ADMIN_PASSWORD},
                          json={"client_id": client_id, "client_secret": client_secret},
                          timeout=15)
        d = r.json()
        return d.get("token", ""), d.get("user_info", {})
    else:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = requests.post("https://iam.api.vngcloud.vn/accounts-api/v2/auth/token",
                          data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {creds}",
                                   "Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        d = r.json()
        return d.get("access_token", ""), d

def gn_api(token, uid, method, path, body=None):
    import requests
    base = "https://hcm-3.api.vngcloud.vn/vserver/vserver-gateway"
    headers = {
        "Authorization":    f"Bearer {token}",
        "Content-Type":     "application/json",
        "portal-user-id":   str(uid),
        "x-portal-user-id": str(uid),
    }
    url = f"{base}/{path}"
    r = requests.request(method, url, json=body, headers=headers, verify=False, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

def parse_list(data):
    if isinstance(data, list): return data
    for k in ("listData", "data", "items", "results"):
        if isinstance(data.get(k), list): return data[k]
    return []

def run_secgroup_alerts():
    from sg_risk_engine import run_sg_risk_detection
    customers = get_all_customers()
    for cust in customers:
        try:
            run_sg_risk_detection(
                customer=cust,
                get_conn_fn=get_conn,
                db_write_fn=db_write_notification,
                database_url=DATABASE_URL,
            )
        except Exception as e:
            print(f"[MONITOR] secgroup error for {cust['name']}: {e}")
            db_write_notification(cust["name"], "[SECGROUP] Lỗi quét Security Group", str(e), "danger")

def run_health_alerts():
    customers = get_all_customers()
    for cust in customers:
        try:
            token, info = fetch_token(cust["client_id"], cust["client_secret"])
            uid = str(info.get("accountId") or info.get("userId", "0"))
            P = cust["project_id"]
            sv, dv = gn_api(token, uid, "GET", f"v2/{P}/servers")
            vms = parse_list(dv) if sv == 200 else []
            shutoff = [v for v in vms if v.get("status") in ("SHUTOFF", "ERROR")]
            if shutoff:
                names = ", ".join(v.get("name", "?") for v in shutoff[:5])
                db_write_notification(cust["name"], f"⚠️ {len(shutoff)} VM không hoạt động", f"Các VM đang SHUTOFF/ERROR: {names}", "warning")
        except Exception as e:
            print(f"[MONITOR] health error for {cust['name']}: {e}")

VMONITOR_BASE = "https://vmonitor.console.vngcloud.vn/vmonitor-api/api"

def vmonitor_get(token, path):
    import requests
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.get(f"{VMONITOR_BASE}{path}", headers=headers, verify=False, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

def run_cpu_ram_alerts():
    CPU_THRESHOLD = 80
    customers = get_all_customers()
    for cust in customers:
        try:
            token, info = fetch_token(cust["client_id"], cust["client_secret"])
            # Get vserver hosts from vMonitor
            st, data = vmonitor_get(token, "/v1/infrastructure/vserver/hosts?name=&page=1&size=50")
            if st != 200:
                print(f"[CPU] vserver/hosts failed: {st}")
                continue
            hosts = data.get("lstData", []) if isinstance(data, dict) else []
            for h in hosts:
                if not h.get("monitor_enabled"):
                    continue
                host_id = h.get("id")
                name = h.get("server_name") or h.get("server_id", "?")
                ms, md = vmonitor_get(token, f"/v1/infrastructure/vserver/hosts/{host_id}/metric")
                print(f"[CPU] {name} metric_status={ms} data={str(md)[:200]}")
                if ms != 200:
                    continue
                cpu_obj = md.get("vServerCPUUsage")
                cpu = float(cpu_obj.get("value", -1)) if isinstance(cpu_obj, dict) else None
                if cpu is not None and cpu >= CPU_THRESHOLD:
                    db_write_notification(
                        cust["name"],
                        f"🔥 CPU cao: {name}",
                        f"VM '{name}' đang dùng {cpu:.1f}% CPU (ngưỡng {CPU_THRESHOLD}%)",
                        "danger"
                    )
        except Exception as e:
            print(f"[MONITOR] cpu_ram error for {cust['name']}: {e}")

SECGROUP_INTERVAL = 1 * 60  # 1 minute
HEALTH_INTERVAL   = 30 * 60
CPU_RAM_INTERVAL  = 5  * 60

def main():
    last = {"secgroup": 0, "health": 0, "cpu_ram": 0}
    while True:
        now = time.time()
        if now - last["secgroup"] >= SECGROUP_INTERVAL:
            run_secgroup_alerts()
            last["secgroup"] = now
        if now - last["health"] >= HEALTH_INTERVAL:
            run_health_alerts()
            last["health"] = now
        if now - last["cpu_ram"] >= CPU_RAM_INTERVAL:
            run_cpu_ram_alerts()
            last["cpu_ram"] = now
        time.sleep(10)

if __name__ == "__main__":
    main()
