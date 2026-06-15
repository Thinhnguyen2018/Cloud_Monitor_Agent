"""
Security Group Risk Detection Engine

Architecture:
  CloudConnector → InventoryCollector → PolicyEngine
  → RiskScoringEngine → AlertGenerator → NotificationService
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICIES_FILE = os.path.join(_DIR, "sg_policies.json")


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class SGRule:
    direction: str          # ingress / egress
    protocol: str           # tcp / udp / icmp / all
    port_min: Optional[int]
    port_max: Optional[int]
    cidr: str
    rule_id: str = ""

    def port_label(self) -> str:
        if self.port_min is None:
            return "all"
        if self.port_min == self.port_max:
            return str(self.port_min)
        return f"{self.port_min}-{self.port_max}"


@dataclass
class SecurityGroup:
    cloud_account: str
    sg_id: str
    name: str
    rules: List[SGRule] = field(default_factory=list)


@dataclass
class PolicyViolation:
    sg: SecurityGroup
    policy_id: str
    policy_name: str
    severity: str
    risk_score: int
    message: str
    recommendation: str
    matched_rule: Optional[SGRule]

    def rule_label(self) -> str:
        r = self.matched_rule
        if not r:
            return "N/A"
        return f"{r.direction.upper()} {r.protocol.upper()} :{r.port_label()} from {r.cidr}"


@dataclass
class SGAlert:
    customer: str
    sg_id: str
    sg_name: str
    policy_id: str
    policy_name: str
    severity: str
    risk_score: int
    message: str
    recommendation: str
    rule_detail: str
    status: str = "OPEN"


# ── Cloud Connector ────────────────────────────────────────────────────────────

class VNGCloudConnector:
    """Fetches raw Security Group data from VNG Cloud vServer API."""

    BASE = "https://hcm-3.api.vngcloud.vn/vserver/vserver-gateway"

    def __init__(self, token: str, uid: str, project_id: str):
        import requests
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "portal-user-id": str(uid),
            "x-portal-user-id": str(uid),
        })
        self._s.verify = False
        self._pid = project_id

    def _get(self, path: str) -> Tuple[int, dict]:
        try:
            r = self._s.get(f"{self.BASE}/{path}", timeout=20)
            return r.status_code, r.json()
        except Exception as e:
            return 0, {"_error": str(e)}

    def list_security_groups(self) -> List[dict]:
        status, data = self._get(f"v2/{self._pid}/secgroups")
        if status != 200:
            return []
        items = data.get("listData") or data.get("data") or data
        return items if isinstance(items, list) else []

    def get_sg_rules(self, sg_id: str) -> List[dict]:
        # Try dedicated rules endpoint first, fall back to detail endpoint
        for path in [
            f"v2/{self._pid}/secgroups/{sg_id}/secGroupRules",
            f"v2/{self._pid}/secgroups/{sg_id}",
        ]:
            status, data = self._get(path)
            if status != 200:
                continue
            d = data if isinstance(data, dict) else {}
            rules = (
                data if isinstance(data, list) else
                d.get("data") if isinstance(d.get("data"), list) else
                d.get("secgroupRuleEntities") or d.get("rules") or d.get("listData") or []
            )
            if isinstance(rules, list) and rules:
                return rules
        return []


# ── Inventory Collector ────────────────────────────────────────────────────────

class InventoryCollector:
    """Fetches and normalizes Security Group inventory from the cloud provider."""

    def __init__(self, connector: VNGCloudConnector, cloud_account: str):
        self._conn = connector
        self._account = cloud_account

    def collect(self) -> List[SecurityGroup]:
        raw_list = self._conn.list_security_groups()
        result = []
        for raw in raw_list:
            sg_id = raw.get("id") or raw.get("uuid", "")
            if not sg_id:
                continue
            # Inline rules (sometimes embedded in list response)
            inline = (
                raw.get("secGroupRuleInfoSet")
                or raw.get("secgroupRuleEntities")
                or raw.get("rules")
                or []
            )
            # Dedicated rules endpoint
            fetched = self._conn.get_sg_rules(sg_id)
            all_raw = fetched or inline
            rules = [self._normalize(r) for r in all_raw if isinstance(r, dict)]
            result.append(SecurityGroup(
                cloud_account=self._account,
                sg_id=sg_id,
                name=raw.get("name", "?"),
                rules=rules,
            ))
        return result

    @staticmethod
    def _normalize(r: dict) -> SGRule:
        direction = (r.get("direction") or "ingress").lower()
        proto_raw = r.get("protocol") or "any"
        protocol  = "all" if proto_raw in ("any", "", None) else proto_raw.lower()
        port_min  = r.get("portRangeMin") if r.get("portRangeMin") is not None else r.get("port_range_min")
        port_max  = r.get("portRangeMax") if r.get("portRangeMax") is not None else r.get("port_range_max")
        cidr      = r.get("remoteIpPrefix") or r.get("remote_ip_prefix") or "0.0.0.0/0"
        return SGRule(
            direction=direction,
            protocol=protocol,
            port_min=int(port_min) if port_min is not None else None,
            port_max=int(port_max) if port_max is not None else None,
            cidr=cidr,
            rule_id=r.get("id") or r.get("uuid", ""),
        )


# ── Policy Engine ──────────────────────────────────────────────────────────────

class PolicyEngine:
    """Evaluates Security Groups against policies loaded from sg_policies.json."""

    def __init__(self, policies_file: str = DEFAULT_POLICIES_FILE):
        with open(policies_file) as f:
            cfg = json.load(f)
        self._policies = cfg["policies"]
        self._scores   = cfg.get("risk_scores", {"CRITICAL": 100, "HIGH": 70, "MEDIUM": 40, "LOW": 10})

    def evaluate(self, sgs: List[SecurityGroup]) -> List[PolicyViolation]:
        violations = []
        for sg in sgs:
            for rule in sg.rules:
                for policy in self._policies:
                    v = self._match(sg, rule, policy)
                    if v:
                        violations.append(v)
        return violations

    def _match(self, sg: SecurityGroup, rule: SGRule, policy: dict) -> Optional[PolicyViolation]:
        cond = policy["conditions"]

        if rule.direction != cond.get("direction", rule.direction):
            return None

        allowed_cidrs = cond.get("cidr", [])
        if allowed_cidrs and rule.cidr not in allowed_cidrs:
            return None

        # Protocol check (used by SG004 "all ports" policy)
        policy_protos = [p.lower() for p in cond.get("protocol", [])]
        if policy_protos and rule.protocol not in policy_protos:
            return None

        # Port check (skip if policy has no port conditions, e.g. SG004)
        policy_ports = cond.get("port", [])
        if policy_ports:
            if rule.port_min is None and rule.port_max is None:
                # Rule has no port restriction → matches any port
                pass
            else:
                matched = any(
                    rule.port_min is not None
                    and rule.port_max is not None
                    and rule.port_min <= p <= rule.port_max
                    for p in policy_ports
                )
                if not matched:
                    return None

        severity = policy["severity"]
        return PolicyViolation(
            sg=sg,
            policy_id=policy["id"],
            policy_name=policy["name"],
            severity=severity,
            risk_score=self._scores.get(severity, 0),
            message=policy["message"],
            recommendation=policy["recommendation"],
            matched_rule=rule,
        )


# ── Risk Scoring Engine ────────────────────────────────────────────────────────

class RiskScoringEngine:
    """Aggregates violations into risk scores and summary statistics."""

    def score_per_sg(self, violations: List[PolicyViolation]) -> dict:
        """Returns {sg_id: score} capped at 100, deduplicated by policy."""
        scores: dict = {}
        for v in violations:
            sid = v.sg.sg_id
            if sid not in scores:
                scores[sid] = {}
            scores[sid][v.policy_id] = max(scores[sid].get(v.policy_id, 0), v.risk_score)
        return {sid: min(sum(p.values()), 100) for sid, p in scores.items()}

    def summary(self, violations: List[PolicyViolation]) -> dict:
        by_sev: dict = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for v in violations:
            by_sev[v.severity] = by_sev.get(v.severity, 0) + 1
        return {"total": len(violations), "by_severity": by_sev}


# ── Alert Generator ────────────────────────────────────────────────────────────

class AlertGenerator:
    """
    Persists alerts to sg_alerts table with full lifecycle (OPEN → RESOLVED).
    Deduplication key: (customer, sg_id, policy_id) where status = OPEN.
    """

    def __init__(self, get_conn_fn, database_url: str = ""):
        self._get_conn = get_conn_fn
        self._is_pg    = bool(database_url)
        self._ph       = "%s" if database_url else "?"

    def ensure_table(self):
        conn = self._get_conn()
        cur  = conn.cursor()
        if self._is_pg:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sg_alerts (
                    id              SERIAL PRIMARY KEY,
                    customer        VARCHAR(255) NOT NULL,
                    sg_id           VARCHAR(255) NOT NULL,
                    sg_name         VARCHAR(255),
                    policy_id       VARCHAR(20)  NOT NULL,
                    policy_name     VARCHAR(255),
                    severity        VARCHAR(20),
                    risk_score      INTEGER DEFAULT 0,
                    status          VARCHAR(20) DEFAULT 'OPEN',
                    message         TEXT,
                    recommendation  TEXT,
                    rule_detail     TEXT,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sg_alerts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer        TEXT NOT NULL,
                    sg_id           TEXT NOT NULL,
                    sg_name         TEXT,
                    policy_id       TEXT NOT NULL,
                    policy_name     TEXT,
                    severity        TEXT,
                    risk_score      INTEGER DEFAULT 0,
                    status          TEXT DEFAULT 'OPEN',
                    message         TEXT,
                    recommendation  TEXT,
                    rule_detail     TEXT,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
        conn.close()

    def upsert(self, alert: SGAlert) -> Tuple[bool, bool]:
        """
        Returns (is_new, was_updated).
        Skips insert if an OPEN alert with same (customer, sg_id, policy_id) exists.
        """
        ph   = self._ph
        conn = self._get_conn()
        cur  = conn.cursor()
        try:
            cur.execute(
                f"SELECT id, rule_detail FROM sg_alerts "
                f"WHERE customer={ph} AND sg_id={ph} AND policy_id={ph} AND status='OPEN' LIMIT 1",
                (alert.customer, alert.sg_id, alert.policy_id),
            )
            existing = cur.fetchone()
            if existing:
                if existing[1] != alert.rule_detail:
                    ts = "NOW()" if self._is_pg else "datetime('now')"
                    cur.execute(
                        f"UPDATE sg_alerts SET rule_detail={ph}, updated_at={ts} WHERE id={ph}",
                        (alert.rule_detail, existing[0]),
                    )
                    conn.commit()
                    conn.close()
                    return False, True
                conn.close()
                return False, False

            cur.execute(
                f"INSERT INTO sg_alerts "
                f"(customer,sg_id,sg_name,policy_id,policy_name,severity,risk_score,status,message,recommendation,rule_detail) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN',{ph},{ph},{ph})",
                (alert.customer, alert.sg_id, alert.sg_name, alert.policy_id, alert.policy_name,
                 alert.severity, alert.risk_score, alert.message, alert.recommendation, alert.rule_detail),
            )
            conn.commit()
            conn.close()
            return True, False
        except Exception:
            conn.close()
            raise

    def resolve_stale(self, customer: str, active_keys: set) -> List[Tuple[str, str]]:
        """Mark OPEN alerts RESOLVED if their (sg_id, policy_id) no longer appears in active violations."""
        ph   = self._ph
        conn = self._get_conn()
        cur  = conn.cursor()
        cur.execute(
            f"SELECT id, sg_id, policy_id FROM sg_alerts WHERE customer={ph} AND status='OPEN'",
            (customer,),
        )
        resolved = []
        ts = "NOW()" if self._is_pg else "datetime('now')"
        for row_id, sg_id, policy_id in cur.fetchall():
            if (sg_id, policy_id) not in active_keys:
                cur.execute(
                    f"UPDATE sg_alerts SET status='RESOLVED', updated_at={ts} WHERE id={ph}",
                    (row_id,),
                )
                resolved.append((sg_id, policy_id))
        conn.commit()
        conn.close()
        return resolved


# ── Notification Service ───────────────────────────────────────────────────────

class NotificationService:
    """
    Pluggable notification channels.
    Currently implemented: DB (for Monitor Agent UI).
    Extend send_* methods to add Slack / Telegram / Email / Teams.
    """

    SEVERITY_TYPE = {"CRITICAL": "danger", "HIGH": "danger", "MEDIUM": "warning", "LOW": "info"}

    def __init__(self, db_write_fn):
        self._db = db_write_fn

    def notify_violation(self, customer: str, v: PolicyViolation):
        title = f"🔴 [{v.policy_id}] {v.policy_name} — {v.sg.name}"
        body  = (
            f"Severity: {v.severity}  |  Risk Score: {v.risk_score}/100\n"
            f"{v.message}\n"
            f"Rule: {v.rule_label()}\n"
            f"Recommendation: {v.recommendation}"
        )
        ntype = self.SEVERITY_TYPE.get(v.severity, "warning")
        self._db(customer, title, body, ntype)

    def notify_scan_summary(self, customer: str, new_count: int, summary: dict):
        if new_count == 0:
            return
        sev = summary.get("by_severity", {})
        title = f"🛡️ Security Scan: {new_count} vi phạm mới phát hiện"
        body  = (
            f"CRITICAL: {sev.get('CRITICAL', 0)}  "
            f"HIGH: {sev.get('HIGH', 0)}  "
            f"MEDIUM: {sev.get('MEDIUM', 0)}  "
            f"LOW: {sev.get('LOW', 0)}"
        )
        self._db(customer, title, body, "danger")

    def notify_resolved(self, customer: str, resolved: list):
        if not resolved:
            return
        title = f"✅ {len(resolved)} cảnh báo Security Group đã được giải quyết"
        body  = "\n".join(f"SG {sg_id[:8]}… policy {pid}" for sg_id, pid in resolved[:10])
        self._db(customer, title, body, "info")


# ── Network Reachability Scanner ──────────────────────────────────────────────

@dataclass
class NetworkViolation:
    vm_id: str
    vm_name: str
    public_ip: str
    sg_id: str
    sg_name: str
    port: int
    protocol: str
    policy_id: str
    policy_name: str
    severity: str
    risk_score: int
    message: str
    recommendation: str


class NetworkScanner:
    """
    Detects open dangerous ports on VMs with public floating IPs.
    Bypasses the need to read SG rules from API (which returns 403).
    Proves that a port IS actually reachable from the internet.
    """

    PROBE_TIMEOUT = 3  # seconds per port

    def __init__(self, connector: "VNGCloudConnector", policies_file: str = DEFAULT_POLICIES_FILE):
        self._connector = connector
        with open(policies_file) as f:
            cfg = json.load(f)
        self._scores = cfg.get("risk_scores", {"CRITICAL": 100, "HIGH": 70, "MEDIUM": 40, "LOW": 10})
        # Build port → policy map from policies that have port conditions
        self._port_policies: dict = {}
        for p in cfg["policies"]:
            for port in p["conditions"].get("port", []):
                if port not in self._port_policies or \
                   self._scores.get(p["severity"], 0) > self._scores.get(self._port_policies[port]["severity"], 0):
                    self._port_policies[port] = p
        # All unique ports to probe
        self._ports_to_probe = sorted(self._port_policies.keys())

    def _list_vms(self) -> List[dict]:
        status, data = self._connector._get(f"v2/{self._connector._pid}/servers")
        if status != 200:
            return []
        items = data.get("listData") or data.get("data") or data
        return items if isinstance(items, list) else []

    def _probe_port(self, ip: str, port: int) -> bool:
        import socket
        try:
            with socket.create_connection((ip, port), timeout=self.PROBE_TIMEOUT):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def scan(self) -> List[NetworkViolation]:
        violations = []
        vms = self._list_vms()

        for vm in vms:
            vm_id   = vm.get("uuid") or vm.get("id", "")
            vm_name = vm.get("name", "?")
            sg_list = vm.get("secGroups") or vm.get("security_groups") or []

            # Collect all floating IPs from internal interfaces
            public_ips = []
            for iface in vm.get("internalInterfaces", []):
                fip = iface.get("floatingIp") or iface.get("floating_ip")
                if fip:
                    public_ips.append(fip)
            for iface in vm.get("externalInterfaces", []):
                fip = iface.get("floatingIp") or iface.get("ip") or iface.get("fixedIp")
                if fip:
                    public_ips.append(fip)

            if not public_ips:
                continue  # No public IP — skip

            sg_id   = sg_list[0].get("uuid", "") if sg_list else vm_id
            sg_name = sg_list[0].get("name", "N/A") if sg_list else "N/A"

            for ip in public_ips:
                for port in self._ports_to_probe:
                    if self._probe_port(ip, port):
                        policy = self._port_policies[port]
                        severity = policy["severity"]
                        violations.append(NetworkViolation(
                            vm_id=vm_id,
                            vm_name=vm_name,
                            public_ip=ip,
                            sg_id=sg_id,
                            sg_name=sg_name,
                            port=port,
                            protocol="tcp",
                            policy_id=policy["id"],
                            policy_name=policy["name"],
                            severity=severity,
                            risk_score=self._scores.get(severity, 0),
                            message=f"{policy['message']} — VM: {vm_name} ({ip}:{port})",
                            recommendation=policy["recommendation"],
                        ))
                        print(f"[NET_SCAN] OPEN port {port} on {vm_name} ({ip})")

        return violations


# ── Pipeline entry point ───────────────────────────────────────────────────────

def _get_token(customer: dict) -> Tuple[str, dict]:
    import requests, base64
    proxy_url      = os.environ.get("PROXY_TOKEN_URL", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin12345")
    if proxy_url:
        r = requests.post(
            proxy_url,
            headers={"Content-Type": "application/json", "X-Proxy-Secret": admin_password},
            json={"client_id": customer["client_id"], "client_secret": customer["client_secret"]},
            timeout=15,
        )
        d = r.json()
        return d.get("token", ""), d.get("user_info", {})
    else:
        creds = base64.b64encode(f"{customer['client_id']}:{customer['client_secret']}".encode()).decode()
        r = requests.post(
            "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        d = r.json()
        return d.get("access_token", ""), d


def run_sg_risk_detection(customer: dict, get_conn_fn, db_write_fn,
                          database_url: str = "",
                          policies_file: str = DEFAULT_POLICIES_FILE):
    """
    Full Security Group Risk Detection pipeline for one customer.

    Steps:
      1. Authenticate
      2. CloudConnector → SG inventory → PolicyEngine (rule-based, may return 0 if API restricted)
      3. NetworkScanner → probe dangerous ports on public IPs (works regardless of API permissions)
      4. RiskScoringEngine → scores and summary
      5. AlertGenerator → upsert/resolve alerts with deduplication
      6. NotificationService → write to DB
    """
    token, info = _get_token(customer)
    uid = str(info.get("accountId") or info.get("userId", "0"))

    connector = VNGCloudConnector(token, uid, customer["project_id"])

    # Step 2a: Rule-based detection (via SG API)
    collector  = InventoryCollector(connector, customer["name"])
    sgs        = collector.collect()
    policy_eng = PolicyEngine(policies_file)
    sg_violations = policy_eng.evaluate(sgs)

    # Step 2b: Network-based detection (port probing)
    net_scanner      = NetworkScanner(connector, policies_file)
    net_violations   = net_scanner.scan()

    # Step 4: Risk scoring across both sources
    scorer  = RiskScoringEngine()
    all_sg_count = len(sg_violations)
    all_net_count = len(net_violations)
    combined_summary = {
        "total": all_sg_count + all_net_count,
        "by_severity": {
            sev: (scorer.summary(sg_violations)["by_severity"].get(sev, 0) +
                  sum(1 for v in net_violations if v.severity == sev))
            for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        }
    }

    # Steps 5–6: Alert persistence + notifications
    alert_gen = AlertGenerator(get_conn_fn, database_url)
    alert_gen.ensure_table()
    notifier  = NotificationService(db_write_fn)

    active_keys = set()
    new_count   = 0
    new_by_sg: dict = {}  # sg_name → list of new violations

    # Process SG policy violations
    for v in sg_violations:
        alert = SGAlert(
            customer=customer["name"],
            sg_id=v.sg.sg_id,
            sg_name=v.sg.name,
            policy_id=v.policy_id,
            policy_name=v.policy_name,
            severity=v.severity,
            risk_score=v.risk_score,
            message=v.message,
            recommendation=v.recommendation,
            rule_detail=v.rule_label(),
        )
        is_new, _ = alert_gen.upsert(alert)
        active_keys.add((v.sg.sg_id, v.policy_id))
        if is_new:
            new_by_sg.setdefault(v.sg.name, []).append(v)
            new_count += 1

    # Process network scan violations
    for nv in net_violations:
        net_sg_id     = f"net-{nv.vm_id}"
        net_policy_id = f"NET-{nv.policy_id}-{nv.port}"
        alert = SGAlert(
            customer=customer["name"],
            sg_id=net_sg_id,
            sg_name=f"{nv.sg_name} → {nv.vm_name}",
            policy_id=net_policy_id,
            policy_name=nv.policy_name,
            severity=nv.severity,
            risk_score=nv.risk_score,
            message=nv.message,
            recommendation=nv.recommendation,
            rule_detail=f"NETWORK PROBE: {nv.public_ip}:{nv.port}/tcp OPEN",
        )
        is_new, _ = alert_gen.upsert(alert)
        active_keys.add((net_sg_id, net_policy_id))
        if is_new:
            label = f"{nv.sg_name} → {nv.vm_name}"
            new_by_sg.setdefault(label, []).append(nv)
            new_count += 1

    # Send one grouped notification per dangerous SG
    for sg_name, viols in new_by_sg.items():
        severities  = [v.severity if hasattr(v, "severity") else v.severity for v in viols]
        worst       = "CRITICAL" if "CRITICAL" in severities else "HIGH" if "HIGH" in severities else "MEDIUM"
        risk_scores = [v.risk_score for v in viols]
        max_score   = max(risk_scores)
        issues      = ", ".join(
            v.policy_name if hasattr(v, "policy_name") else v.policy_name
            for v in viols
        )
        title = f"🔴 Security Group nguy hiểm: {sg_name}"
        body  = (
            f"Phát hiện {len(viols)} quy tắc không an toàn:\n"
            + "\n".join(
                f"  • [{v.severity}] {v.policy_name}: {v.message}"
                for v in viols
            )
            + f"\n\nRisk Score: {max_score}/100"
            + f"\nKhuyến nghị: Kiểm tra và giới hạn quyền truy cập ngay."
        )
        ntype = "danger" if worst in ("CRITICAL", "HIGH") else "warning"
        db_write_fn(customer["name"], title, body, ntype)

    resolved = alert_gen.resolve_stale(customer["name"], active_keys)
    notifier.notify_resolved(customer["name"], resolved)

    print(
        f"[SG_RISK] {customer['name']}: "
        f"{len(sgs)} SGs, {all_sg_count} rule-violations, "
        f"{all_net_count} network-violations, "
        f"{new_count} new alerts, {len(resolved)} resolved"
    )
    return sg_violations + [None] * len(net_violations)  # unified return
