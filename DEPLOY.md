# Finly — Deployment Guide

## Project structure
```
finly/
├── Dockerfile          # Railway build
├── railway.toml        # Railway config
├── bot/
│   ├── bot.py          # Telegram bot + starts FastAPI
│   └── requirements.txt
├── api/
│   ├── __init__.py
│   └── main.py         # FastAPI backend (all endpoints)
└── webapp/
    ├── index.html      # Telegram Mini App (deploy to Netlify)
    └── netlify.toml
```

---

## Step 1 — Push to GitHub
Create a new repo and push this folder to it.

## Step 2 — Railway setup

1. Go to https://railway.app → New Project → Deploy from GitHub repo
2. Add PostgreSQL: **+ New** → **Database** → **PostgreSQL**
   - Railway will auto-set `DATABASE_URL` — no action needed
3. Set these environment variables on your service:
   ```
   BOT_TOKEN    = <your token from @BotFather>
   WEBAPP_URL   = https://your-netlify-app.netlify.app
   API_PORT     = 8080
   ```
4. Railway will build and deploy automatically.
   Your Railway public URL will look like: `https://finly-xxxx.up.railway.app`

## Step 3 — Webapp (Netlify)

1. Open `webapp/index.html`
2. Find this line near the top:
   ```js
   window.FINLY_API_URL = "REPLACE_WITH_RAILWAY_URL";
   ```
3. Replace `REPLACE_WITH_RAILWAY_URL` with your Railway URL, e.g.:
   ```js
   window.FINLY_API_URL = "https://finly-xxxx.up.railway.app";
   ```
4. Go to https://netlify.com → drag-and-drop the `webapp/` folder
5. Copy the Netlify URL (e.g. `https://finly-app.netlify.app`)
6. Update `WEBAPP_URL` in Railway env vars with this URL

## Step 4 — BotFather

Set your bot's menu button to the Netlify URL:
1. Open @BotFather → /mybots → your bot
2. Bot Settings → Menu Button → set URL to your Netlify URL

## Step 5 — Test

Send `/start` to your bot → tap the button → app should load with your data.

---

## API health check
Visit: `https://your-railway-url.up.railway.app/api/health`
Should return: `{"status":"ok","ts":"..."}`

## Env variables summary
| Variable     | Where   | Value                        |
|--------------|---------|------------------------------|
| BOT_TOKEN    | Railway | From @BotFather              |
| WEBAPP_URL   | Railway | Your Netlify URL             |
| API_PORT     | Railway | 8080                         |
| DATABASE_URL | Railway | Auto-set by Railway Postgres |
