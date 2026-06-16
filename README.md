# GreenNode Cloud Monitor Agent

An AI-powered cloud infrastructure management agent for VNG Cloud, built for **Claw-a-thon 2026**. Interact with your cloud resources through natural language — monitor health, manage VMs, control storage, configure networking, and automate operations, all from a conversational interface.

---

## Features

### AI Chat Interface
- Natural language commands in Vietnamese and English
- Intent detection: understands "tắt VM web-server lúc 2h sáng" automatically
- Context-aware responses powered by GreenNode MaaS AI

### VM Management
| Action | Example Command |
|--------|----------------|
| Start / Stop / Reboot | "tắt vm web-server", "khởi động lại db-01" |
| Create VM | "tạo vm tên web-01 ubuntu 22.04 2vcpu 4gb" |
| Delete VM | "xóa vm test-server" |
| Rename | "đổi tên vm old-name thành new-name" |
| Snapshot | "tạo snapshot cho vm web-server" |

### Storage Management
| Action | Example Command |
|--------|----------------|
| Attach volume | "gắn volume data-disk vào vm web-server" |
| Detach volume | "gỡ volume data-disk khỏi vm web-server" |
| Delete volume | "xóa volume old-disk" |

### Networking
| Action | Example Command |
|--------|----------------|
| Associate Floating IP | "gắn IP 103.x.x.x vào vm web-server" |
| Disassociate Floating IP | "gỡ floating IP khỏi vm web-server" |
| Add Security Group rule | "mở port 443 cho sg default" |
| Remove Security Group rule | "xóa rule [rule-id] khỏi sg default" |

### Scheduled Actions
- Schedule any VM action at a specific time: "hẹn tắt vm web-server lúc 2:00 ngày 15/06"
- View and cancel scheduled jobs
- Persistent across restarts

### Automated Monitoring
| Job | Interval | Description |
|-----|----------|-------------|
| Security Group Audit | Every 15 min | Detect overly permissive firewall rules (public SSH, RDP, DB ports) |
| CPU/RAM Alerts | Every 5 min | Alert when CPU exceeds 80% threshold via vMonitor API |
| Health Check | Every 30 min | Detect SHUTOFF or ERROR VMs |

### Multi-tenant Dashboard
- Manage multiple VNG Cloud customer accounts from one interface
- Per-customer notifications and audit log
- Real-time alert panel for danger/warning events

---

## Architecture

```
AgentBase Runtime (Docker)
    ├── Flask API (port 8080, gunicorn)
    │   ├── GreenNode MaaS AI — intent detection & natural language responses
    │   ├── APScheduler — scheduled VM actions (stop/start/reboot at set time)
    │   └── SQLite / PostgreSQL — customers, notifications, audit log, token cache
    └── monitor.py (background process)
        ├── Security Group risk scanner — every 1 min
        ├── CPU/RAM threshold alerts via vMonitor API — every 5 min
        └── VM health checker — every 30 min
```

**Stack:** Python 3.11 · Flask · APScheduler · PostgreSQL · Docker · GreenNode AgentBase · GreenNode MaaS AI · VNG Cloud vMonitor API

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_USERNAME` | Yes | Admin login username |
| `ADMIN_PASSWORD` | Yes | Admin login password |
| `DATABASE_URL` | No | PostgreSQL URL (falls back to SQLite) |
| `PROXY_TOKEN_URL` | No | Token proxy URL to avoid IAM rate limits |
| `GN_MAAS_API_KEY` | Yes | GreenNode MaaS API key for AI responses |
| `GN_MAAS_MODEL` | No | MaaS model ID (default: google/gemma-4-31b-it) |
| `FLASK_SECRET_KEY` | Yes | Secret key for Flask session |

---

## Local Development

```bash
# Clone & install
git clone https://github.com/Thinhnguyen2018/Monitor_Agent.git
cd Monitor_Agent
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 app:app
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
| `POST` | `/api/chat` | AI chat with intent detection |
| `GET/POST` | `/api/customers` | List / add customers |
| `DELETE` | `/api/customers/<name>` | Remove customer |
| `POST` | `/api/resources` | Fetch all cloud resources |
| `POST` | `/api/action` | Execute VM / volume / network action |
| `GET/POST` | `/api/schedules` | List / create scheduled jobs |
| `DELETE` | `/api/schedules/<id>` | Cancel scheduled job |
| `GET` | `/api/notifications` | List notifications |
| `POST` | `/api/notifications/read` | Mark notifications as read |
| `GET` | `/api/alerts` | Get unresolved warning/danger alerts |
| `POST` | `/api/alerts/<id>/resolve` | Resolve an alert |
| `GET` | `/api/audit` | View action history |
| `GET` | `/api/dashboard/stats` | Dashboard summary |

---

## Project Structure

| File / Folder | Role |
|---------------|------|
| `app.py` | Main Flask application — AI chat, VM/network/storage actions, API endpoints |
| `monitor.py` | Background monitoring process — CPU/RAM alerts, health checks |
| `sg_risk_engine.py` | Security Group risk detection engine — policy evaluation & alerts |
| `sg_policies.json` | Security Group risk policies (SSH, RDP, DB, all-ports exposure rules) |
| `start.sh` | Container entrypoint — starts gunicorn + monitor.py in parallel |
| `Dockerfile.agentbase` | Production image for GreenNode AgentBase deployment |
| `Dockerfile` | Local development image for self-hosted Docker/server deployment |
| `docker-compose.yml` | Local development stack (app + environment) |
| `gunicorn_config.py` | Gunicorn server configuration for local/server deployment |
| `requirements.txt` | Python dependencies |
| `static/` | Frontend — single-page dashboard (HTML/CSS/JS) |
| `references/` | Static reference data — VM flavors, images, volume types |
| `.github/workflows/` | CI/CD — GitHub Actions build & push to Docker Hub on every commit |

---

## Deployment

Deployed on **GreenNode AgentBase** — VNG Cloud's AI agent runtime platform.

- **CI/CD:** GitHub Actions builds and pushes on every merge to `main`
- **Image:** `nguyenngocgiathinh/greennode-agent:<commit-sha>`
- **Runtime:** `Clawcathon Monitor Agent` on GreenNode AgentBase

---

## Team

**Team 43** — Claw-a-thon 2026 · Track: Automation & Integration
