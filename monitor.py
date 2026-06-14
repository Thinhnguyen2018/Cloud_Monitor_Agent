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
        cur.execute(f"SELECT id FROM notifications WHERE customer={ph} AND title={ph} AND resolved=0 AND created_at >= {time_expr} LIMIT 1", (customer, title))
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
    url = PROXY_TOKEN_URL if PROXY_TOKEN_URL else "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token"
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(url, data={"grant_type": "client_credentials"},
                      headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    d = r.json()
    return d.get("access_token", ""), d

def gn_api(token, uid, method, path, body=None):
    import requests
    base = "https://hcm-3.api.vngcloud.vn/vserver/vserver-gateway"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-User-Id": str(uid)}
    url = f"{base}/{path}"
    r = requests.request(method, url, json=body, headers=headers, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}

def parse_list(data):
    if isinstance(data, list): return data
    for k in ("listData", "data", "items", "results"):
        if isinstance(data.get(k), list): return data[k]
    return []

_DANGEROUS_PORTS = {22: "SSH", 3389: "RDP", 23: "Telnet", 3306: "MySQL", 5432: "PostgreSQL"}

def is_dangerous_rule(rule):
    direction = (rule.get("direction") or "").lower()
    if direction != "ingress": return None
    remote = rule.get("remoteIpPrefix") or rule.get("remote_ip_prefix") or ""
    if remote not in ("0.0.0.0/0", "::/0", ""): return None
    proto = (rule.get("protocol") or "").lower()
    port_min = rule.get("portRangeMin") if rule.get("portRangeMin") is not None else rule.get("port_range_min")
    port_max = rule.get("portRangeMax") if rule.get("portRangeMax") is not None else rule.get("port_range_max")
    if proto in ("any", "") or proto is None:
        return "Tất cả port mở từ 0.0.0.0/0"
    if port_min is not None and port_max is not None:
        if int(port_min) <= 1 and int(port_max) >= 65534:
            return "Tất cả port mở từ 0.0.0.0/0"
        for port, svc in _DANGEROUS_PORTS.items():
            if int(port_min) <= port <= int(port_max):
                return f"Port {port} ({svc}) mở từ 0.0.0.0/0"
    return None

def run_secgroup_alerts():
    customers = get_all_customers()
    for cust in customers:
        try:
            token, info = fetch_token(cust["client_id"], cust["client_secret"])
            uid = str(info.get("accountId") or info.get("userId", "0"))
            P = cust["project_id"]
            sv, sd = gn_api(token, uid, "GET", f"v2/{P}/secgroups")
            secgroups = parse_list(sd) if sv == 200 else []
            warnings = []
            for sg in secgroups:
                sg_name = sg.get("name", "?")
                sg_id = sg.get("uuid") or sg.get("id", "")
                if not sg_id: continue
                sv2, rd = gn_api(token, uid, "GET", f"v2/{P}/secgroups/{sg_id}")
                rules = (rd.get("secgroupRuleEntities") or rd.get("rules") or []) if sv2 == 200 else []
                inline = sg.get("secGroupRuleInfoSet") or sg.get("secgroupRuleEntities") or []
                for rule in (rules or inline):
                    msg = is_dangerous_rule(rule)
                    if msg:
                        warnings.append(f"[{sg_name}] {msg}")
            if warnings:
                detail = "\n".join(warnings[:10])
                db_write_notification(cust["name"], f"🔴 {len(warnings)} quy tắc Security Group không an toàn", detail, "danger")
        except Exception as e:
            print(f"[MONITOR] secgroup error for {cust['name']}: {e}")
            db_write_notification(cust["name"], "[DEBUG] SECGROUP_ALERT error", str(e), "danger")

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

SECGROUP_INTERVAL = 60
HEALTH_INTERVAL   = 30 * 60
CPU_RAM_INTERVAL  = 5  * 60

def main():
    print("[MONITOR] Ready")
    last = {"secgroup": 0, "health": 0, "cpu_ram": 0}
    while True:
        now = time.time()
        if now - last["secgroup"] >= SECGROUP_INTERVAL:
            run_secgroup_alerts()
            last["secgroup"] = now
        if now - last["health"] >= HEALTH_INTERVAL:
            run_health_alerts()
            last["health"] = now
        time.sleep(10)

if __name__ == "__main__":
    main()
