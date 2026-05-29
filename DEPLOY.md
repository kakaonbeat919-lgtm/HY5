# PallaPay v5 — Railway Deployment Guide

## Quick Deploy

1. Railway dashboard mein **New Project → Deploy from GitHub** ya **Upload**
2. **Variables tab** mein yeh set karo:

| Variable | Value | Notes |
|---|---|---|
| `BOT_TOKEN` | `1234:ABCxyz...` | @BotFather se |
| `ADMIN_IDS` | `123456789` | @userinfobot se pata karo |
| `ADMIN_TOKEN` | koi bhi secret string | Admin panel ke liye |
| `DATABASE_URL` | auto-set | PostgreSQL add karo |

3. **Add PostgreSQL** → Railway automatically `DATABASE_URL` set karta hai

## v5 Improvements (from v4)

- **Connection pooling** — `ThreadedConnectionPool` (1–10 conns), har request pe naya conn nahi banta
- **DB Indexes** — `status`, `user_email`, `created_at` pe index, queries fast
- **Bot single-start guard** — multiple workers mein bot sirf ek baar start hoga
- **Thread-safe state** — bot state dict ko `threading.Lock` se protect kiya
- **Input validation** — amount, status, JSON validation improve kiya
- **Retry logic** — Telegram API calls pe 2x retry with backoff
- **Message length guard** — 4096 char Telegram limit handle kiya
- **Polling backoff** — error pe 1→2→4...30s backoff, CPU waste nahi
- **Better logging** — `logging` module, timestamps, structured errors
- **`/setaed` command** — AED rate bot se set ho sakta hai
- **`python-dotenv`** — local dev ke liye `.env` support
- **Gunicorn threads 4→8** — concurrent requests better handle

## Local Testing

```bash
cp .env.example .env
# .env mein apni values bharo
pip install -r requirements.txt
python app.py
```

## Health Check

`GET /health` — Railway health check ke liye, DB connection verify karta hai
