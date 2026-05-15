"""
Finly — FastAPI Backend
Handles all data persistence for the Telegram Mini App.
Runs alongside bot.py in the same Railway service.
"""

import os, json, logging, hashlib, hmac
from datetime import datetime, date, timedelta
from typing import Optional
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
API_PORT     = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("finly.api")

# ── DB connection pool ────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()

# ── Schema setup ──────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           BIGINT PRIMARY KEY,
    first_name   TEXT NOT NULL DEFAULT '',
    last_name    TEXT NOT NULL DEFAULT '',
    username     TEXT NOT NULL DEFAULT '',
    language     TEXT NOT NULL DEFAULT 'ru',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    streak       INT NOT NULL DEFAULT 0,
    streak_record INT NOT NULL DEFAULT 0,
    xp           INT NOT NULL DEFAULT 0,
    level        INT NOT NULL DEFAULT 1,
    last_log_date DATE,
    notifs       BOOLEAN NOT NULL DEFAULT TRUE,
    weekly_report BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS transactions (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL CHECK (type IN ('expense','income')),
    cat_icon    TEXT NOT NULL DEFAULT '💰',
    cat_name    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    amount      BIGINT NOT NULL,
    date_str    DATE NOT NULL,
    time_str    TEXT NOT NULL DEFAULT '00:00',
    currency    TEXT NOT NULL DEFAULT 'UZS',
    recurring   BOOLEAN NOT NULL DEFAULT FALSE,
    account     TEXT NOT NULL DEFAULT 'Карта',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS drafts (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL DEFAULT 'expense',
    cat_icon    TEXT NOT NULL DEFAULT '❓',
    cat_name    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    amount      BIGINT NOT NULL DEFAULT 0,
    date_str    DATE NOT NULL,
    time_str    TEXT NOT NULL DEFAULT '00:00',
    currency    TEXT NOT NULL DEFAULT 'UZS',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budgets (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    icon        TEXT NOT NULL DEFAULT '📦',
    limit_amt   BIGINT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#16A34A',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT NOT NULL CHECK (type IN ('expense','income')),
    icon        TEXT NOT NULL,
    name        TEXT NOT NULL,
    sort_order  INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xp_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    icon        TEXT NOT NULL DEFAULT '⭐',
    label       TEXT NOT NULL,
    sub         TEXT NOT NULL DEFAULT '',
    pts         INT NOT NULL,
    date_str    DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS achievements (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ach_id      TEXT NOT NULL,
    earned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, ach_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_user   ON transactions(user_id, date_str DESC);
CREATE INDEX IF NOT EXISTS idx_drafts_user         ON drafts(user_id);
CREATE INDEX IF NOT EXISTS idx_budgets_user        ON budgets(user_id);
CREATE INDEX IF NOT EXISTS idx_categories_user     ON categories(user_id, type);
CREATE INDEX IF NOT EXISTS idx_xp_log_user         ON xp_log(user_id, date_str DESC);
"""

DEFAULT_EXP_CATS = [
    ("🛒","Продукты"), ("🏠","Жильё"), ("🚌","Транспорт"), ("🍕","Кафе"),
    ("💊","Здоровье"), ("🎬","Развлечения"), ("👕","Одежда"),
    ("📚","Образование"), ("🤷","Забыл"), ("•••","Прочее"),
]
DEFAULT_INC_CATS = [
    ("💼","Зарплата"), ("💹","Подработка"), ("🏦","Инвестиции"), ("🎁","Подарок"),
]
ALL_ACHIEVEMENTS = [
    {"id":"fire7",    "name":"Огонь",        "icon":"🔥","bg":"#FFF7ED","desc":"7 дней подряд",            "xp":100},
    {"id":"fire21",   "name":"Чемпион",      "icon":"🏆","bg":"#FFFBEB","desc":"21 день подряд",           "xp":300},
    {"id":"first_tx", "name":"Первый шаг",   "icon":"👣","bg":"#EFF6FF","desc":"Первая запись",            "xp":50},
    {"id":"tx10",     "name":"Аналитик",     "icon":"📊","bg":"#F0FDF4","desc":"10 записей",               "xp":75},
    {"id":"tx100",    "name":"Профи",        "icon":"💎","bg":"#FAF5FF","desc":"100 записей",              "xp":200},
    {"id":"budget30", "name":"Бюджетист",    "icon":"🎯","bg":"#FAF5FF","desc":"30 дней без превышений",   "xp":150},
    {"id":"saver",    "name":"Копилка",      "icon":"💰","bg":"#F0FDF4","desc":"Накопить 1 млн сум",       "xp":200},
    {"id":"millioner","name":"Миллионер",    "icon":"🤑","bg":"#FFF7ED","desc":"Накопить 10 млн сум",      "xp":500},
    {"id":"night",    "name":"Ночной страж", "icon":"🌙","bg":"#EFF6FF","desc":"Запись в 00:00–06:00",     "xp":50},
    {"id":"draft5",   "name":"Черновик",     "icon":"📝","bg":"#F4F5F7","desc":"Завершить 5 черновиков",   "xp":80},
]

def init_db():
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
    conn.close()
    log.info("DB schema ready")

# ── Telegram auth ─────────────────────────────────────────────────────────────
def validate_init_data(init_data: str) -> Optional[dict]:
    """Verify Telegram WebApp initData and return parsed user dict."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        from urllib.parse import unquote
        params = {}
        for part in init_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = unquote(v)
        received_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        user_json = params.get("user", "{}")
        return json.loads(user_json)
    except Exception as e:
        log.warning(f"initData validation error: {e}")
        return None

def require_user(x_init_data: str = Header(default="")) -> dict:
    user = validate_init_data(x_init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData — open via Telegram")
    return user

# ── Helpers ───────────────────────────────────────────────────────────────────
def row_to_tx(r) -> dict:
    return {
        "id":        r["id"],
        "type":      r["type"],
        "cat":       r["cat_icon"],
        "catName":   r["cat_name"],
        "note":      r["note"],
        "amount":    r["amount"],
        "dateStr":   str(r["date_str"]),
        "time":      r["time_str"],
        "currency":  r["currency"],
        "recurring": r["recurring"],
        "account":   r["account"],
    }

def row_to_draft(r) -> dict:
    return {
        "id":      r["id"],
        "type":    r["type"],
        "cat":     r["cat_icon"],
        "catName": r["cat_name"],
        "note":    r["note"],
        "amount":  r["amount"],
        "dateStr": str(r["date_str"]),
        "time":    r["time_str"],
        "currency":r["currency"],
        "isDraft": True,
    }

def row_to_budget(r, spent: int = 0) -> dict:
    return {
        "id":    r["id"],
        "name":  r["name"],
        "icon":  r["icon"],
        "limit": r["limit_amt"],
        "spent": spent,
        "color": r["color"],
    }

def calc_budget_spent(cur, user_id: int, budget_name: str) -> int:
    today = date.today()
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) FROM transactions
        WHERE user_id=%s AND type='expense' AND cat_name=%s
          AND date_str >= date_trunc('month', CURRENT_DATE)::date
    """, (user_id, budget_name))
    return int(cur.fetchone()[0])

def upsert_user(conn, tg_user: dict) -> tuple[dict, bool]:
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        existing = cur.fetchone()
        if existing:
            return dict(existing), False
        # New user — insert with default categories
        cur.execute("""
            INSERT INTO users (id, first_name, last_name, username, language)
            VALUES (%s,%s,%s,%s,%s)
        """, (uid, tg_user.get("first_name",""), tg_user.get("last_name",""),
              tg_user.get("username",""), tg_user.get("language_code","ru")))
        for i,(icon,name) in enumerate(DEFAULT_EXP_CATS):
            cur.execute("INSERT INTO categories(user_id,type,icon,name,sort_order) VALUES(%s,'expense',%s,%s,%s)",
                        (uid,icon,name,i))
        for i,(icon,name) in enumerate(DEFAULT_INC_CATS):
            cur.execute("INSERT INTO categories(user_id,type,icon,name,sort_order) VALUES(%s,'income',%s,%s,%s)",
                        (uid,icon,name,i))
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        new_user = dict(cur.fetchone())
        conn.commit()
        log.info(f"New user: {uid} @{tg_user.get('username','')}")
        return new_user, True

def check_and_update_streak(conn, user_id: int) -> tuple[int, int, bool]:
    """Returns (new_streak, xp_earned, streak_continued)."""
    with conn.cursor() as cur:
        cur.execute("SELECT streak, streak_record, last_log_date, xp, level FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        today = date.today()
        last = u["last_log_date"]
        streak = u["streak"]
        record = u["streak_record"]
        xp_earned = 10  # base xp per transaction
        streak_continued = False

        if last is None or last < today - timedelta(days=1):
            # Reset or start streak
            streak = 1
        elif last == today - timedelta(days=1):
            # Continued streak
            streak += 1
            xp_earned += 20  # streak bonus
            streak_continued = True
        elif last == today:
            pass  # already logged today, no streak change
        
        new_record = max(record, streak)
        new_xp = u["xp"] + xp_earned
        new_level = max(1, new_xp // 500 + 1)

        cur.execute("""
            UPDATE users SET streak=%s, streak_record=%s, xp=%s, level=%s, last_log_date=%s
            WHERE id=%s
        """, (streak, new_record, new_xp, new_level, today, user_id))
        return streak, xp_earned, streak_continued

def add_xp_log(cur, user_id: int, icon: str, label: str, sub: str, pts: int):
    cur.execute("""
        INSERT INTO xp_log (user_id, icon, label, sub, pts, date_str)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (user_id, icon, label, sub, pts, date.today()))

def check_achievements(conn, user_id: int) -> list[str]:
    """Check and award any newly unlocked achievements. Returns list of new ach IDs."""
    with conn.cursor() as cur:
        cur.execute("SELECT ach_id FROM achievements WHERE user_id=%s", (user_id,))
        earned_ids = {r["ach_id"] for r in cur.fetchall()}
        cur.execute("SELECT streak, xp, last_log_date FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s", (user_id,))
        tx_count = cur.fetchone()["cnt"]
        
        newly_earned = []
        checks = [
            ("fire7",    u["streak"] >= 7),
            ("fire21",   u["streak"] >= 21),
            ("first_tx", tx_count >= 1),
            ("tx10",     tx_count >= 10),
            ("tx100",    tx_count >= 100),
        ]
        for ach_id, condition in checks:
            if condition and ach_id not in earned_ids:
                cur.execute("INSERT INTO achievements(user_id, ach_id) VALUES(%s,%s)", (user_id, ach_id))
                ach = next((a for a in ALL_ACHIEVEMENTS if a["id"] == ach_id), None)
                if ach:
                    bonus = ach["xp"]
                    cur.execute("UPDATE users SET xp=xp+%s WHERE id=%s", (bonus, user_id))
                    add_xp_log(cur, user_id, ach["icon"], f"Значок: {ach['name']}", ach["desc"], bonus)
                newly_earned.append(ach_id)
        return newly_earned

# ── Pydantic models ───────────────────────────────────────────────────────────
class TxCreate(BaseModel):
    type:      str
    cat_icon:  str
    cat_name:  str
    note:      str = ""
    amount:    int
    date_str:  str
    time_str:  str = "00:00"
    currency:  str = "UZS"
    recurring: bool = False
    account:   str = "Карта"

class TxUpdate(BaseModel):
    type:      Optional[str] = None
    cat_icon:  Optional[str] = None
    cat_name:  Optional[str] = None
    note:      Optional[str] = None
    amount:    Optional[int] = None
    date_str:  Optional[str] = None
    time_str:  Optional[str] = None
    currency:  Optional[str] = None
    recurring: Optional[bool] = None
    account:   Optional[str] = None

class DraftCreate(BaseModel):
    type:     str = "expense"
    cat_icon: str = "❓"
    cat_name: str = ""
    note:     str = ""
    amount:   int = 0
    date_str: str
    time_str: str = "00:00"
    currency: str = "UZS"

class BudgetCreate(BaseModel):
    name:      str
    icon:      str = "📦"
    limit_amt: int
    color:     str = "#16A34A"

class BudgetUpdate(BaseModel):
    name:      Optional[str] = None
    icon:      Optional[str] = None
    limit_amt: Optional[int] = None
    color:     Optional[str] = None

class CategoryCreate(BaseModel):
    type: str
    icon: str
    name: str

class UserPatch(BaseModel):
    notifs:        Optional[bool] = None
    weekly_report: Optional[bool] = None

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="Finly API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}

# ── Auth / bootstrap ──────────────────────────────────────────────────────────
@app.post("/api/auth")
def auth(tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Called on webapp open. Returns full user state in one shot."""
    user, is_new = upsert_user(conn, tg_user)
    uid = user["id"]

    with conn.cursor() as cur:
        # transactions (last 200)
        cur.execute("""
            SELECT * FROM transactions WHERE user_id=%s
            ORDER BY date_str DESC, created_at DESC LIMIT 200
        """, (uid,))
        txs = [row_to_tx(r) for r in cur.fetchall()]

        # drafts
        cur.execute("SELECT * FROM drafts WHERE user_id=%s ORDER BY created_at DESC", (uid,))
        drafts = [row_to_draft(r) for r in cur.fetchall()]

        # budgets with spent
        cur.execute("SELECT * FROM budgets WHERE user_id=%s ORDER BY created_at", (uid,))
        budgets_raw = cur.fetchall()
        budgets = []
        for b in budgets_raw:
            spent = calc_budget_spent(cur, uid, b["name"])
            budgets.append(row_to_budget(b, spent))

        # categories
        cur.execute("SELECT * FROM categories WHERE user_id=%s ORDER BY type, sort_order", (uid,))
        cats_raw = cur.fetchall()
        exp_cats = [{"icon":r["icon"],"name":r["name"]} for r in cats_raw if r["type"]=="expense"]
        inc_cats = [{"icon":r["icon"],"name":r["name"]} for r in cats_raw if r["type"]=="income"]

        # achievements
        cur.execute("SELECT ach_id, earned_at FROM achievements WHERE user_id=%s", (uid,))
        earned_map = {r["ach_id"]: r["earned_at"] for r in cur.fetchall()}
        achievements = []
        for a in ALL_ACHIEVEMENTS:
            earned = a["id"] in earned_map
            entry = {**a, "earned": earned, "locked": False}
            if earned:
                entry["date"] = earned_map[a["id"]].strftime("%d.%m.%Y")
            else:
                entry["progress"] = _ach_progress(a["id"], user, len(txs))
                entry["label"] = _ach_label(a["id"], user, len(txs))
                entry["locked"] = _ach_locked(a["id"], user, len(txs))
            achievements.append(entry)

        # xp log (last 50)
        cur.execute("""
            SELECT * FROM xp_log WHERE user_id=%s
            ORDER BY created_at DESC LIMIT 50
        """, (uid,))
        xp_log = [{"icon":r["icon"],"label":r["label"],"sub":r["sub"],"pts":r["pts"],"dateStr":str(r["date_str"])} for r in cur.fetchall()]

    return {
        "user": {
            "id":           user["id"],
            "first_name":   user["first_name"],
            "last_name":    user["last_name"],
            "username":     user["username"],
            "streak":       user["streak"],
            "streak_record":user["streak_record"],
            "xp":           user["xp"],
            "level":        user["level"],
            "notifs":       user["notifs"],
            "weekly_report":user["weekly_report"],
            "last_log_date":str(user["last_log_date"]) if user["last_log_date"] else None,
        },
        "txs":          txs,
        "drafts":       drafts,
        "budgets":      budgets,
        "exp_cats":     exp_cats,
        "inc_cats":     inc_cats,
        "achievements": achievements,
        "xp_log":       xp_log,
        "is_new":       is_new,
    }

def _ach_progress(ach_id, user, tx_count):
    m = {"fire7": (user["streak"],7), "fire21": (user["streak"],21), "tx10":(tx_count,10), "tx100":(tx_count,100)}
    if ach_id in m:
        v,t = m[ach_id]
        return min(100, round(v/t*100))
    return 0

def _ach_label(ach_id, user, tx_count):
    m = {"fire7": f"{user['streak']}/7 дней", "fire21": f"{user['streak']}/21 дней",
         "tx10": f"{tx_count}/10 записей", "tx100": f"{tx_count}/100 записей"}
    return m.get(ach_id, "")

def _ach_locked(ach_id, user, tx_count):
    locked = {"night", "millioner", "budget30", "draft5", "saver"}
    return ach_id in locked

# ── Transactions ──────────────────────────────────────────────────────────────
@app.post("/api/transactions")
def create_transaction(body: TxCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,recurring,account)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.type, body.cat_icon, body.cat_name, body.note,
              body.amount, body.date_str, body.time_str, body.currency,
              body.recurring, body.account))
        tx = row_to_tx(cur.fetchone())

    # Streak + XP
    streak, xp_earned, streak_cont = check_and_update_streak(conn, uid)

    with conn.cursor() as cur:
        add_xp_log(cur, uid, "➕", f"Добавлена операция", f"{body.time_str} · {body.cat_name}", 10)
        if streak_cont:
            add_xp_log(cur, uid, "🔥", f"Серия сохранена", f"Streak ×{streak}", 20)

    newly_earned = check_achievements(conn, uid)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT xp, level, streak FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()

    return {
        "tx":          tx,
        "xp_earned":   xp_earned,
        "total_xp":    u["xp"],
        "level":       u["level"],
        "streak":      u["streak"],
        "new_achievements": newly_earned,
    }

@app.put("/api/transactions/{tx_id}")
def update_transaction(tx_id: int, body: TxUpdate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    for f, col in [("type","type"),("cat_icon","cat_icon"),("cat_name","cat_name"),
                   ("note","note"),("amount","amount"),("date_str","date_str"),
                   ("time_str","time_str"),("currency","currency"),
                   ("recurring","recurring"),("account","account")]:
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{col}=%s")
            vals.append(v)
    if not fields:
        raise HTTPException(400, "No fields to update")
    vals += [tx_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE transactions SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Transaction not found")
        tx = row_to_tx(row)
    conn.commit()
    return tx

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transactions WHERE id=%s AND user_id=%s RETURNING id", (tx_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Transaction not found")
    conn.commit()
    return {"deleted": tx_id}

# ── Drafts ────────────────────────────────────────────────────────────────────
@app.post("/api/drafts")
def create_draft(body: DraftCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO drafts (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.type, body.cat_icon, body.cat_name, body.note,
              body.amount, body.date_str, body.time_str, body.currency))
        draft = row_to_draft(cur.fetchone())
    conn.commit()
    return draft

@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM drafts WHERE id=%s AND user_id=%s RETURNING id", (draft_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Draft not found")
    conn.commit()

    with conn.cursor() as cur:
        add_xp_log(cur, uid, "✅", "Черновик завершён", "", 15)
        cur.execute("UPDATE users SET xp=xp+15 WHERE id=%s", (uid,))
    conn.commit()
    return {"deleted": draft_id, "xp_earned": 15}

# ── Budgets ───────────────────────────────────────────────────────────────────
@app.post("/api/budgets")
def create_budget(body: BudgetCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO budgets (user_id,name,icon,limit_amt,color)
            VALUES (%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.name, body.icon, body.limit_amt, body.color))
        b = cur.fetchone()
        spent = calc_budget_spent(cur, uid, b["name"])
        budget = row_to_budget(b, spent)
    conn.commit()
    return budget

@app.put("/api/budgets/{budget_id}")
def update_budget(budget_id: int, body: BudgetUpdate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    for f, col in [("name","name"),("icon","icon"),("limit_amt","limit_amt"),("color","color")]:
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{col}=%s")
            vals.append(v)
    if not fields:
        raise HTTPException(400, "No fields to update")
    vals += [budget_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE budgets SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Budget not found")
        spent = calc_budget_spent(cur, uid, row["name"])
        budget = row_to_budget(row, spent)
    conn.commit()
    return budget

@app.delete("/api/budgets/{budget_id}")
def delete_budget(budget_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE id=%s AND user_id=%s RETURNING id", (budget_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Budget not found")
    conn.commit()
    return {"deleted": budget_id}

# ── Categories ────────────────────────────────────────────────────────────────
@app.post("/api/categories")
def create_category(body: CategoryCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM categories WHERE user_id=%s AND type=%s", (uid, body.type))
        order = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO categories (user_id,type,icon,name,sort_order)
            VALUES (%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.type, body.icon, body.name, order))
        r = cur.fetchone()
        cat = {"id": r["id"], "icon": r["icon"], "name": r["name"], "type": r["type"]}
    conn.commit()
    return cat

@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM categories WHERE id=%s AND user_id=%s RETURNING id", (cat_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Category not found")
    conn.commit()
    return {"deleted": cat_id}

# ── User settings ─────────────────────────────────────────────────────────────
@app.patch("/api/user")
def patch_user(body: UserPatch, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    if body.notifs is not None:
        fields.append("notifs=%s"); vals.append(body.notifs)
    if body.weekly_report is not None:
        fields.append("weekly_report=%s"); vals.append(body.weekly_report)
    if not fields:
        raise HTTPException(400, "No fields to update")
    vals.append(uid)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE users SET {','.join(fields)} WHERE id=%s RETURNING *", vals)
        u = cur.fetchone()
    conn.commit()
    return {"notifs": u["notifs"], "weekly_report": u["weekly_report"]}

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.get("/api/leaderboard")
def leaderboard(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, first_name, username, xp, level, streak
            FROM users ORDER BY xp DESC LIMIT 20
        """)
        rows = cur.fetchall()
    result = []
    for i, r in enumerate(rows):
        result.append({
            "rank":       i + 1,
            "id":         r["id"],
            "name":       r["first_name"] or r["username"] or "User",
            "username":   r["username"],
            "xp":         r["xp"],
            "level":      r["level"],
            "streak":     r["streak"],
            "is_you":     r["id"] == uid,
        })
    return result

# ── Analytics summary ─────────────────────────────────────────────────────────
@app.get("/api/analytics")
def analytics(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        # This month totals
        cur.execute("""
            SELECT type, COALESCE(SUM(amount),0) as total
            FROM transactions WHERE user_id=%s
            AND date_str >= date_trunc('month', CURRENT_DATE)::date
            GROUP BY type
        """, (uid,))
        month_totals = {r["type"]: int(r["total"]) for r in cur.fetchall()}

        # Today XP
        cur.execute("""
            SELECT COALESCE(SUM(pts),0) as total FROM xp_log
            WHERE user_id=%s AND date_str=CURRENT_DATE
        """, (uid,))
        today_xp = int(cur.fetchone()["total"])

        # This week XP
        cur.execute("""
            SELECT COALESCE(SUM(pts),0) as total FROM xp_log
            WHERE user_id=%s AND date_str >= date_trunc('week', CURRENT_DATE)::date
        """, (uid,))
        week_xp = int(cur.fetchone()["total"])

    return {
        "month_income":  month_totals.get("income", 0),
        "month_expense": month_totals.get("expense", 0),
        "today_xp":      today_xp,
        "week_xp":       week_xp,
    }
