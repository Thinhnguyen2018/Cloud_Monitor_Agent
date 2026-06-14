# GreenNode Cloud Monitor Agent

An AI-powered cloud infrastructure management agent for VNG Cloud, built for **Claw-a-thon 2026**. Interact with your cloud resources through natural language — monitor health, manage VMs, control storage, configure networking, and automate operations, all from a conversational interface.

---

## Features

### AI Chat Interface
- Natural language commands in Vietnamese and English
- Intent detection: understands "tắt VM web-server lúc 2h sáng" automatically
- Context-aware responses powered by Claude AI

### VM Management
| Action | Example Command |
|--------|----------------|
| Start / Stop / Reboot | "tắt vm web-server", "khởi động lại db-01" |
| Create VM | "tạo vm mới" |
| Delete VM | "xóa vm test-server" |
| Resize (change flavor) | "nâng cấp vm app-01 sang flavor mới" |
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
| CPU/RAM Alerts | Every 5 min | Alert when thresholds exceeded |
| Security Group Audit | Every 15 min | Detect overly permissive firewall rules |
| Health Check | Every 30 min | Detect SHUTOFF or ERROR VMs |

### Multi-tenant Dashboard
- Manage multiple VNG Cloud customer accounts from one interface
- Per-customer notifications and audit log
- Push notifications (Web Push / PWA)

---

## Architecture

```
AgentBase Runtime (Docker)
    └── Flask API (port 8080)
        ├── Claude AI — intent detection & natural language responses
        ├── APScheduler — background monitoring & scheduled actions
        ├── SQLite / PostgreSQL — credentials, notifications, token cache
        └── Token Proxy → VNG Server → VNG Cloud IAM API
```

**Stack:** Python 3.11 · Flask · APScheduler · SQLite/PostgreSQL · Docker · GreenNode AgentBase · Claude AI

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
git clone https://github.com/Thinhnguyen2018/Monitor_Agent.git
cd Monitor_Agent
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run
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
| `POST` | `/api/chat` | AI chat with intent detection |
| `GET/POST` | `/api/customers` | List / add customers |
| `DELETE` | `/api/customers/<name>` | Remove customer |
| `POST` | `/api/resources` | Fetch all cloud resources |
| `POST` | `/api/action` | Execute VM / volume / network action |
| `POST/GET` | `/api/schedule` | Create / list scheduled jobs |
| `DELETE` | `/api/schedule/<id>` | Cancel scheduled job |
| `GET` | `/api/notifications` | List alerts & notifications |
| `GET` | `/api/audit` | View action history |
| `GET` | `/api/vmonitor/metrics/<vm_id>` | CPU/RAM metrics |
| `GET` | `/api/dashboard/stats` | Dashboard summary |

---

## Deployment

Deployed on **GreenNode AgentBase** — VNG Cloud's AI agent runtime platform.

- **Runtime:** `greennode-agent-43`
- **CI/CD:** GitHub Actions builds and pushes on every merge to `main`
- **Image:** `nguyenngocgiathinh/greennode-agent:<commit-sha>`

---

## Team

**Team 43** — Claw-a-thon 2026 · Track: Automation & Integration


