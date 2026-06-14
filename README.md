# GreenNode Cloud Monitor Agent

An AI-powered monitoring agent for VNG Cloud infrastructure, built for the **Claw-a-thon 2026** competition. Automatically monitors server health, detects anomalies, sends real-time alerts, and enables VM lifecycle management — all through a conversational AI interface deployed on GreenNode AgentBase.

---

## Features

- **Real-time VM Monitoring** — tracks CPU, RAM usage every 5 minutes; alerts when thresholds exceeded
- **Health Alerts** — detects VMs in SHUTOFF/ERROR state every 30 minutes
- **Security Group Audits** — scans for overly permissive firewall rules every 15 minutes
- **VM Lifecycle Control** — start, stop, reboot VMs via natural language commands
- **Multi-tenant** — manage multiple VNG Cloud customer accounts from a single dashboard
- **Scheduled Actions** — automate VM operations at specific times
- **Audit Log** — full history of all actions performed
- **Persistent Token Cache** — SQLite-backed IAM token storage survives container restarts

---

## Architecture

```
AgentBase Runtime (Docker)
    └── Flask API (port 8080)
        ├── APScheduler (health/secgroup/cpu_ram jobs)
        ├── SQLite / PostgreSQL (credentials, notifications, token cache)
        └── Token Proxy → VNG Server → IAM API
```

**Stack:** Python 3.11 · Flask · APScheduler · SQLite/PostgreSQL · Docker · GreenNode AgentBase

---

## Monitoring Schedule

| Job | Interval | Description |
|-----|----------|-------------|
| CPU/RAM Alerts | Every 5 min | Alert when CPU or RAM exceeds threshold |
| SecGroup Audit | Every 15 min | Detect open/dangerous firewall rules |
| Health Check | Every 30 min | Detect SHUTOFF or ERROR VMs |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_USERNAME` | Yes | Admin login username |
| `ADMIN_PASSWORD` | Yes | Admin login password |
| `DATABASE_URL` | No | PostgreSQL URL (falls back to SQLite) |
| `PROXY_TOKEN_URL` | No | Token proxy URL to avoid IAM rate limits |
| `GN_MAAS_API_KEY` | No | GreenNode MaaS API key |

---

## Local Development

```bash
# Clone & install
git clone https://github.com/nguyenngocgiathinh/greennode-agent.git
cd greennode-agent
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
python app.py
# or
gunicorn -w 1 -b 0.0.0.0:8000 app:app
```

---

## Docker

```bash
# Build
docker build -f Dockerfile.agentbase -t greennode-agent .

# Run
docker run -p 8080:8080 --env-file .env greennode-agent
```

Image: [`nguyenngocgiathinh/greennode-agent`](https://hub.docker.com/r/nguyenngocgiathinh/greennode-agent)

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/login` | Admin login |
| `GET` | `/api/customers` | List all customers |
| `POST` | `/api/customers` | Add customer |
| `GET` | `/api/vms` | List VMs for a customer |
| `POST` | `/api/vm/action` | Start / Stop / Reboot VM |
| `GET` | `/api/notifications` | List alerts & notifications |
| `GET` | `/api/audit-log` | View action history |
| `POST` | `/api/proxy/token` | IAM token proxy (internal) |

---

## Deployment on AgentBase

This agent is deployed on **GreenNode AgentBase** — VNG Cloud's AI agent runtime platform.

- **Runtime:** `greennode-agent-43`
- **Image:** `nguyenngocgiathinh/greennode-agent:<sha>`
- **CI/CD:** GitHub Actions builds and pushes on every merge to `main`

---

## Team

**Team 43** — Claw-a-thon 2026 · Track: Automation & Integration


