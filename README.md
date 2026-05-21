# GreenNode AI Agent

Chatbot quản lý hạ tầng GreenNode (VNG Cloud) với dữ liệu real-time.

## Cấu trúc

```
greennode-agent/
├── app.py              # Flask backend (API endpoints)
├── requirements.txt    # Python dependencies
├── .env.example        # Template biến môi trường
├── Dockerfile          # Docker container
├── nginx.conf          # Nginx reverse proxy config
├── deploy.sh           # Deploy script cho Ubuntu VPS
└── static/
    └── index.html      # Chatbot UI
```

## Endpoints

| Method | Path | Mô tả |
|--------|------|--------|
| POST | /api/auth | Xác thực GreenNode credentials |
| POST | /api/chat | Chat — lấy dữ liệu real-time + Claude AI |
| POST | /api/resources | Lấy toàn bộ resources (không qua AI) |
| POST | /api/action | Thực hiện action (start/stop/reboot VM...) |
| GET  | /health | Health check |

## Deploy lên VPS

### Cách 1 — Script tự động (Ubuntu/Debian)
```bash
git clone / scp project lên VPS
cd greennode-agent
chmod +x deploy.sh
sudo ./deploy.sh
```

### Cách 2 — Docker
```bash
# Tạo .env từ .env.example
cp .env.example .env
nano .env  # điền ANTHROPIC_API_KEY

# Build & run
docker build -t greennode-agent .
docker run -d -p 8000:8000 --env-file .env greennode-agent
```

### Cách 3 — Thủ công
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Chỉnh .env: điền ANTHROPIC_API_KEY
gunicorn app:app -w 2 -b 0.0.0.0:8000
```

## Biến môi trường (.env)

```env
ANTHROPIC_API_KEY=sk-ant-xxxxx   # Bắt buộc — lấy tại console.anthropic.com
FLASK_SECRET_KEY=random-string   # Tuỳ chọn
```

## Tính năng

- **Real-time data**: Mỗi tin nhắn = 1 lần gọi GreenNode API → luôn chính xác
- **Token cache**: Token GN được cache 25 phút, tự gia hạn
- **Multi-user**: Mỗi user dùng credentials riêng, cách ly hoàn toàn
- **Actions**: Start/stop/reboot VM, attach/detach SG, tạo snapshot
- **Security**: API key chỉ ở server, không bao giờ ra browser

## Lấy ANTHROPIC_API_KEY

1. Vào https://console.anthropic.com
2. API Keys → Create Key
3. Copy key → paste vào .env
