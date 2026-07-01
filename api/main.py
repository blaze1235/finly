"""
Finly — FastAPI Backend
Handles all data persistence for the Telegram Mini App.
Runs alongside bot.py in the same Railway service.
"""

import os, re, json, logging, hashlib, hmac, asyncio, secrets, uuid, plistlib, base64
from datetime import datetime, date, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Version ───────────────────────────────────────────────────────────────────
# Pre-1.0: increment patch on every deploy.  Switch to 1.0.0 when bot confirmed stable.
VERSION = "0.1.4"

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL   = os.getenv("DATABASE_URL", "")
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")  # e.g. "FinlyTrackerBot"
ADMIN_TOKEN    = os.getenv("ADMIN_TOKEN", "")   # protects /api/admin/* endpoints
API_PORT       = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ── STT client (Groq preferred, OpenAI fallback) ──────────────────────────────
_stt_client   = None
_stt_provider = None
if GROQ_API_KEY:
    try:
        from groq import Groq as _GroqClient
        _stt_client   = _GroqClient(api_key=GROQ_API_KEY)
        _stt_provider = "groq"
        logging.getLogger("finly.api").info("Groq STT ready (whisper-large-v3-turbo)")
    except ImportError:
        logging.getLogger("finly.api").warning("groq package not installed")
if _stt_client is None and OPENAI_API_KEY:
    try:
        from openai import OpenAI as _OAIClient
        _stt_client   = _OAIClient(api_key=OPENAI_API_KEY)
        _stt_provider = "openai"
        logging.getLogger("finly.api").info("OpenAI Whisper STT ready")
    except ImportError:
        logging.getLogger("finly.api").warning("openai package not installed")

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
    account     TEXT NOT NULL DEFAULT 'Наличка',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT 'Наличка';

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

ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS friends (
    id          BIGSERIAL PRIMARY KEY,
    requester_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(requester_id, addressee_id)
);
CREATE INDEX IF NOT EXISTS idx_friends_addressee ON friends(addressee_id);

CREATE TABLE IF NOT EXISTS social_debts (
    id           BIGSERIAL PRIMARY KEY,
    creator_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    counterpart_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    amount       BIGINT NOT NULL,
    due_date     DATE,
    note         TEXT NOT NULL DEFAULT '',
    debt_type    TEXT NOT NULL DEFAULT 'borrowed' CHECK (debt_type IN ('borrowed','lent')),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','settled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_social_debts_creator ON social_debts(creator_id);
CREATE INDEX IF NOT EXISTS idx_social_debts_counterpart ON social_debts(counterpart_id);

-- Migration: fix status constraint to include all valid values (handles old deploys)
DO $$
BEGIN
    ALTER TABLE social_debts DROP CONSTRAINT IF EXISTS social_debts_status_check;
    ALTER TABLE social_debts ADD CONSTRAINT social_debts_status_check
        CHECK (status IN ('pending','approved','rejected','settled'));
EXCEPTION WHEN others THEN NULL;
END$$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS notif_pref_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS report_pref_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS shared_goals (
    id           BIGSERIAL PRIMARY KEY,
    creator_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    partner_id   BIGINT REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    emoji        TEXT NOT NULL DEFAULT '🎯',
    target       BIGINT NOT NULL,
    saved        BIGINT NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    deadline     DATE,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','completed','rejected')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shared_goals_creator ON shared_goals(creator_id);
CREATE INDEX IF NOT EXISTS idx_shared_goals_partner ON shared_goals(partner_id);

CREATE TABLE IF NOT EXISTS shared_goal_contributions (
    id         BIGSERIAL PRIMARY KEY,
    goal_id    BIGINT NOT NULL REFERENCES shared_goals(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount     BIGINT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sgc_goal ON shared_goal_contributions(goal_id);

-- Multi-member shared goals (supports >2 participants)
CREATE TABLE IF NOT EXISTS shared_goal_members (
    id        BIGSERIAL PRIMARY KEY,
    goal_id   BIGINT NOT NULL REFERENCES shared_goals(id) ON DELETE CASCADE,
    user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status    TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(goal_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_sgm_goal ON shared_goal_members(goal_id);
CREATE INDEX IF NOT EXISTS idx_sgm_user ON shared_goal_members(user_id);

-- Link transactions to shared goal contributions for two-way sync
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS linked_goal_id BIGINT REFERENCES shared_goals(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS linked_contribution_id BIGINT REFERENCES shared_goal_contributions(id) ON DELETE SET NULL;

-- Reminder time per user (default 18:00 Tashkent)
ALTER TABLE users ADD COLUMN IF NOT EXISTS reminder_time TEXT NOT NULL DEFAULT '18:00';

-- Rank visibility: everyone / friends / nobody
ALTER TABLE users ADD COLUMN IF NOT EXISTS rank_visible TEXT NOT NULL DEFAULT 'everyone';

-- Referral system
ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_code TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT REFERENCES users(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ref_code ON users(ref_code) WHERE ref_code IS NOT NULL;

-- Personal SMS webhook token for iOS Shortcuts integration
ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_token TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS budget_plan_active BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS budget_plan_days INT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS budget_plan_daily BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS budget_plan_activated_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notif_overtake BOOLEAN NOT NULL DEFAULT TRUE;
-- Streak restore: admin can offer a one-time "log today, get your streak back"
ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_restore_to INT;
CREATE TABLE IF NOT EXISTS feedback (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS rating SMALLINT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS source TEXT; -- 'blast' | NULL=organic
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS source TEXT; -- 'shortcut' | 'voice' | 'sms' | NULL=app
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS original_amount BIGINT; -- amount in original currency (e.g. USD cents → store as integer dollars*100? No — just store as-is)
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS original_currency TEXT; -- 'USD','EUR',etc. NULL means UZS
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS exchange_rate INT; -- rate used at time of logging (e.g. 12200 for 1 USD)

-- Bank card connections (for balance tracking & SMS auto-logging)
CREATE TABLE IF NOT EXISTS bank_cards (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bank         TEXT NOT NULL DEFAULT '',
    card_last4   TEXT NOT NULL DEFAULT '',
    alias        TEXT NOT NULL DEFAULT '',
    balance      BIGINT NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'UZS',
    auto_log          BOOLEAN NOT NULL DEFAULT TRUE,
    include_in_balance BOOLEAN NOT NULL DEFAULT TRUE,
    color             TEXT NOT NULL DEFAULT '#6366F1',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE bank_cards ADD COLUMN IF NOT EXISTS include_in_balance BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS idx_bank_cards_user ON bank_cards(user_id);

-- Pending SMS transactions awaiting user confirmation
CREATE TABLE IF NOT EXISTS sms_pending (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    raw_text     TEXT NOT NULL DEFAULT '',
    bank         TEXT NOT NULL DEFAULT '',
    card_last4   TEXT NOT NULL DEFAULT '',
    amount       BIGINT NOT NULL,
    tx_type      TEXT NOT NULL DEFAULT 'expense',
    merchant     TEXT NOT NULL DEFAULT '',
    balance      BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sms_pending_user ON sms_pending(user_id);

CREATE TABLE IF NOT EXISTS personal_goals (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji        TEXT NOT NULL DEFAULT '🎯',
    name         TEXT NOT NULL,
    target       BIGINT NOT NULL DEFAULT 0,
    saved        BIGINT NOT NULL DEFAULT 0,
    duration     INTEGER NOT NULL DEFAULT 6,
    start_year   INTEGER,
    start_month  INTEGER,
    deadline_year  INTEGER,
    deadline_month INTEGER,
    done         BOOLEAN NOT NULL DEFAULT FALSE,
    done_date    DATE,
    contributions JSONB NOT NULL DEFAULT '[]',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_personal_goals_user ON personal_goals(user_id);

CREATE TABLE IF NOT EXISTS personal_debts (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    amount     BIGINT NOT NULL DEFAULT 0,
    type       TEXT NOT NULL DEFAULT 'borrowed',
    note       TEXT NOT NULL DEFAULT '',
    due_date   DATE,
    paid       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_personal_debts_user ON personal_debts(user_id);
ALTER TABLE personal_debts ADD COLUMN IF NOT EXISTS paid_amount BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS savings_jars (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    emoji      TEXT NOT NULL DEFAULT '🏺',
    balance    BIGINT NOT NULL DEFAULT 0,
    target     BIGINT,
    note       TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_savings_jars_user ON savings_jars(user_id);

-- Remote-control task queue: Telegram /claude command → cron picks up & executes
CREATE TABLE IF NOT EXISTS claude_tasks (
    id         BIGSERIAL PRIMARY KEY,
    task       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
    result     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Native iOS app: short-lived link codes + long-lived device bearer tokens
CREATE TABLE IF NOT EXISTS link_codes (
    code       TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS device_tokens (
    token      TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform   TEXT NOT NULL DEFAULT 'ios',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE TABLE IF NOT EXISTS linked_groups (
    chat_id    BIGINT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    chat_title TEXT,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    can_reply     BOOLEAN NOT NULL DEFAULT FALSE,
    is_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Migration: live table predates these columns (CREATE IF NOT EXISTS won't add them)
ALTER TABLE business_connections ADD COLUMN IF NOT EXISTS can_reply  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE business_connections ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Donations: user pledges a monthly amount, pays P2P off-platform, self-reports
-- in-app (auto-confirmed on trust). Admin can decline a specific entry as a
-- safety valve if a self-report turns out to be false.
CREATE TABLE IF NOT EXISTS donation_pledges (
    user_id        BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    monthly_amount BIGINT NOT NULL DEFAULT 0,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS donations (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount       BIGINT NOT NULL,
    period       TEXT NOT NULL,                          -- 'YYYY-MM'
    status       TEXT NOT NULL DEFAULT 'confirmed',       -- 'confirmed' | 'declined'
    note         TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_donations_user ON donations(user_id);
CREATE INDEX IF NOT EXISTS idx_donations_period ON donations(period);
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
    # Transaction milestones
    {"id":"first_tx",   "name":"Первый шаг",      "icon":"👣","bg":"#EFF6FF","desc":"Первая запись",                  "xp":50},
    {"id":"tx10",       "name":"Аналитик",         "icon":"📊","bg":"#F0FDF4","desc":"10 записей",                    "xp":75},
    {"id":"tx50",       "name":"Опытный",          "icon":"📈","bg":"#F0FDF4","desc":"50 записей",                    "xp":150},
    {"id":"tx100",      "name":"Профи",            "icon":"💎","bg":"#FAF5FF","desc":"100 записей",                   "xp":250},
    {"id":"tx500",      "name":"Ветеран",          "icon":"🎖","bg":"#FAF5FF","desc":"500 записей",                   "xp":600},
    {"id":"tx1000",     "name":"Хронист",          "icon":"📜","bg":"#FAF5FF","desc":"1000 записей",                  "xp":1000},
    # Streak — 4 milestones, well spaced
    {"id":"fire7",      "name":"Огонь",            "icon":"🔥","bg":"#FFF7ED","desc":"7 дней подряд",                 "xp":100},
    {"id":"streak30",   "name":"Месяц",            "icon":"📆","bg":"#F0FDF4","desc":"30 дней подряд",                "xp":350},
    {"id":"streak100",  "name":"Легенда",          "icon":"👑","bg":"#FAF5FF","desc":"100 дней подряд",               "xp":1000},
    {"id":"streak200",  "name":"Бессмертный",      "icon":"♾️","bg":"#EDE9FE","desc":"200 дней подряд",               "xp":2000},
    # Level milestones
    {"id":"level5",     "name":"Растёт",           "icon":"🌱","bg":"#F0FDF4","desc":"Достичь 5 уровня",              "xp":150},
    {"id":"level10",    "name":"Мастер",           "icon":"🌟","bg":"#FFFBEB","desc":"Достичь 10 уровня",             "xp":300},
    # Goals & debts
    {"id":"goal1",      "name":"Мечтатель",        "icon":"🌟","bg":"#FFFBEB","desc":"Первая цель выполнена",         "xp":150},
    {"id":"goal5",      "name":"Целеустремлённый", "icon":"🚀","bg":"#EFF6FF","desc":"5 целей выполнено",             "xp":350},
    {"id":"goal10",     "name":"Достигатор",       "icon":"🎯","bg":"#F0FDF4","desc":"10 целей выполнено",            "xp":600},
    {"id":"debt1",      "name":"Честный",          "icon":"🤝","bg":"#F0FDF4","desc":"Первый долг погашен",           "xp":100},
    {"id":"debt5",      "name":"Без долгов",       "icon":"💫","bg":"#FAF5FF","desc":"5 долгов погашено",             "xp":300},
    {"id":"debt10",     "name":"Финансист",        "icon":"💼","bg":"#F0FDF4","desc":"10 долгов погашено",            "xp":500},
    # Social & referrals
    {"id":"friend1",    "name":"Социальный",       "icon":"🤗","bg":"#EFF6FF","desc":"Первый друг в Finly",           "xp":80},
    {"id":"referral1",  "name":"Пригласил",        "icon":"🤝","bg":"#EFF6FF","desc":"Первый приглашённый друг",      "xp":100},
    {"id":"referral5",  "name":"Амбассадор",       "icon":"🌟","bg":"#FFFBEB","desc":"5 приглашённых друзей",         "xp":300},
    # Feature discovery (first use, not exploitable)
    {"id":"voice1",     "name":"Голос",            "icon":"🎙","bg":"#EFF6FF","desc":"Первая голосовая запись",       "xp":60},
    {"id":"tx_voice5",  "name":"Диктор",           "icon":"🎤","bg":"#EFF6FF","desc":"5 голосовых записей",           "xp":120},
    {"id":"finlyfast1", "name":"Молния",           "icon":"⚡","bg":"#EDE9FE","desc":"Первая запись FinlyFast",       "xp":60},
    {"id":"finlyfast10","name":"Скорость",         "icon":"🚀","bg":"#EDE9FE","desc":"10 записей FinlyFast",          "xp":150},
    {"id":"savings5",   "name":"Коллекционер",     "icon":"🏦","bg":"#EFF6FF","desc":"5 копилок создано",             "xp":150},
    {"id":"pro_sub",    "name":"Pro",              "icon":"💎","bg":"#EDE9FE","desc":"Оформить подписку Pro",          "xp":500},
    # Donations
    {"id":"donor1",     "name":"Меценат",          "icon":"💜","bg":"#FBEAF0","desc":"Первое пожертвование",           "xp":150},
    {"id":"donor3",     "name":"Постоянный донор", "icon":"🎗","bg":"#FAF5FF","desc":"3 месяца подряд",                "xp":400},
    {"id":"donor12",    "name":"Ангел-хранитель",  "icon":"🏅","bg":"#FFFBEB","desc":"12 месяцев подряд",              "xp":1200},
    # Behaviour — high thresholds to prevent easy earning
    {"id":"early_5",    "name":"Жаворонок",        "icon":"🌤","bg":"#FFFBEB","desc":"10 записей до 9 утра",          "xp":120},
    {"id":"night_5",    "name":"Сова",             "icon":"🦉","bg":"#EFF6FF","desc":"10 записей после полуночи",     "xp":120},
    {"id":"big_spender","name":"Крупняк",          "icon":"💸","bg":"#FFF7ED","desc":"3 записи на 500к+ сум",         "xp":100},
    {"id":"account3",   "name":"Банкир",           "icon":"🏦","bg":"#F0FDF4","desc":"5 разных счётов",               "xp":120},
    {"id":"cats_used5", "name":"Эксперт трат",     "icon":"🎨","bg":"#FAF5FF","desc":"8+ категорий в одном месяце",  "xp":100},
    {"id":"months3",    "name":"Верный",           "icon":"📅","bg":"#F0FDF4","desc":"Записи 3 месяца подряд",        "xp":250},
    {"id":"months6",    "name":"Привычка",         "icon":"🏅","bg":"#FFFBEB","desc":"Записи 6 месяцев подряд",       "xp":500},
    {"id":"note20",     "name":"Летописец",        "icon":"✍️","bg":"#EFF6FF","desc":"Заметки в 30+ записях",         "xp":150},
]

def get_conn_with_timeout(timeout: int = 5):
    """Connect with an explicit timeout so retries happen quickly."""
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=timeout,
    )

def init_db():
    """Connect to DB with retries — Railway DB may take a moment to accept connections."""
    import time as _time
    for attempt in range(1, 31):  # retry up to 30 times (~2.5 min)
        try:
            conn = get_conn_with_timeout(5)  # fail fast: 5s timeout per attempt
            with conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA)
            conn.close()
            log.info("DB schema ready")
            return
        except Exception as e:
            log.warning(f"DB not ready (attempt {attempt}/30): {e}")
            _time.sleep(5)
    raise RuntimeError("Could not connect to DB after 30 attempts")

# ── Telegram auth ─────────────────────────────────────────────────────────────
def validate_init_data(init_data: str) -> Optional[dict]:
    """Verify Telegram WebApp initData and return parsed user dict.
    Robust against URL-encoding variants and minor whitespace differences.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        from urllib.parse import unquote, unquote_plus
        params = {}
        # Handle both & and %26 as separators (some iOS versions encode & differently)
        raw = init_data.strip()
        for part in raw.split("&"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                # Try both unquote and unquote_plus to handle all encoding variants
                params[k.strip()] = unquote(v)
        received_hash = params.pop("hash", "")
        if not received_hash:
            log.warning("initData missing hash field")
            return None
        # Build data_check_string per Telegram spec
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            # Log only first 20 chars to help debug without exposing tokens
            log.warning(f"initData hash mismatch — received={received_hash[:20]}... expected={expected[:20]}...")
            return None
        user_json = params.get("user", "{}")
        user = json.loads(user_json)
        if not user.get("id"):
            log.warning("initData user has no id")
            return None
        if "start_param" in params:
            user["__start_param"] = params["start_param"]
        return user
    except Exception as e:
        log.warning(f"initData validation error: {e}")
        return None

def _user_from_bearer(authorization: str) -> Optional[dict]:
    """Resolve a native-app bearer token to a user dict via device_tokens table."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    try:
        conn = get_conn_with_timeout()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT u.id, u.first_name, u.last_name, u.username, u.language
                    FROM device_tokens d JOIN users u ON u.id = d.user_id
                    WHERE d.token = %s
                """, (token,))
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute("UPDATE device_tokens SET last_used=NOW() WHERE token=%s", (token,))
            conn.commit()
            return {"id": row["id"], "first_name": row.get("first_name", ""),
                    "last_name": row.get("last_name", ""), "username": row.get("username", ""),
                    "language_code": row.get("language", "ru")}
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"bearer auth error: {e}")
        return None

def require_user(x_init_data: str = Header(default=""),
                 authorization: str = Header(default="")) -> dict:
    # Path 1: Telegram WebApp initData (mini app)
    user = validate_init_data(x_init_data)
    if user:
        return user
    # Path 2: native iOS app bearer token
    user = _user_from_bearer(authorization)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Invalid Telegram initData — open via Telegram")

import httpx

async def send_tg_message(user_id: int, text: str, reply_markup: dict = None):
    """Send a Telegram message to a user via bot, with optional inline keyboard."""
    if not BOT_TOKEN:
        return
    try:
        payload = {"chat_id": user_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=5,
            )
    except Exception as e:
        log.warning(f"Failed to send TG message to {user_id}: {e}")

def send_tg_message_sync(user_id: int, text: str, reply_markup: dict = None):
    """Synchronous Telegram send — safe to call from sync route handlers."""
    if not BOT_TOKEN:
        return
    try:
        payload = {"chat_id": user_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=8)
    except Exception as e:
        log.warning(f"sync TG send failed to {user_id}: {e}")

def send_feature_guide(user_id: int):
    """Send a one-time feature guide to a newly-onboarded user."""
    kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}}]]}
    send_tg_message_sync(user_id,
        "🎉 <b>Добро пожаловать в Finly!</b>\n"
        "Вот что умеет приложение — попробуй каждую функцию:\n\n"
        "🏁 <b>Цели</b>\n"
        "Копи на мечту — телефон, отпуск, подушку безопасности. Finly считает, сколько осталось и когда достигнешь цели.\n\n"
        "🎯 <b>Бюджеты</b>\n"
        "Ставь лимит на категорию (например, 500 000 на еду в месяц). Мы предупредим, когда подходишь к лимиту.\n\n"
        "💡 <b>Умный бюджет</b>\n"
        "Скажи, на сколько дней нужно растянуть деньги — Finly посчитает дневной лимит и отделит обязательные траты.\n\n"
        "🤝 <b>Долги</b>\n"
        "Записывай, кто должен тебе и кому должен ты. Никогда не забудешь вернуть или забрать деньги.\n\n"
        "💰 <b>Копилки</b>\n"
        "Откладывай на отдельные счета — деньги «заморожены» и не путаются с основным балансом.\n\n"
        "⚡ <b>FinlyFast</b>\n"
        "Записывай расход за 2 секунды прямо с виджета iPhone — без открытия приложения.\n\n"
        "📊 <b>Отчёты</b>\n"
        "Смотри, куда уходят деньги: диаграммы по категориям, доход/расход за месяц, прогнозы.\n\n"
        "🔥 <b>Серии и рейтинг</b>\n"
        "Веди записи каждый день — копи серию, зарабатывай XP, соревнуйся с друзьями.\n\n"
        "Начни с первой записи — жми кнопку 👇", kb)

async def send_tg_photo_url(user_id: int, photo_url: str, caption: str = "", reply_markup: dict = None):
    """Send a photo by URL to a Telegram user."""
    if not BOT_TOKEN:
        return
    payload = {"chat_id": user_id, "photo": photo_url, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", json=payload)
    except Exception as e:
        log.warning(f"sendPhoto uid={user_id}: {e}")

def _build_pie_chart_url(labels: list, values: list, title: str = "") -> str:
    """Build a QuickChart.io pie chart URL."""
    import urllib.parse, json as _json
    colors = ["#6366F1","#4ADE80","#F87171","#FBBF24","#60A5FA","#A78BFA","#34D399","#FB923C","#F472B6","#94A3B8"]
    config = {
        "type": "doughnut",
        "data": {
            "labels": labels,
            "datasets": [{"data": values, "backgroundColor": colors[:len(values)], "borderWidth": 2, "borderColor": "#1C1C1E"}]
        },
        "options": {
            "plugins": {
                "title": {"display": bool(title), "text": title, "color": "#FFFFFF", "font": {"size": 16, "weight": "bold"}},
                "legend": {"position": "bottom", "labels": {"color": "#AAAAAA", "font": {"size": 11}, "padding": 12}}
            },
            "layout": {"padding": 10}
        }
    }
    chart_json = _json.dumps(config, separators=(",",":"))
    base_url = "https://quickchart.io/chart"
    params = urllib.parse.urlencode({"c": chart_json, "backgroundColor": "#111111", "width": 500, "height": 350, "format": "png"})
    return f"{base_url}?{params}"

# ── Voice / STT helpers ───────────────────────────────────────────────────────
import tempfile

async def transcribe_ogg_bytes(ogg_bytes: bytes) -> str | None:
    """Transcribe OGG voice bytes using Groq or OpenAI Whisper. Returns text or None."""
    if not _stt_client:
        return None
    import asyncio
    loop = asyncio.get_event_loop()
    def _do():
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(ogg_bytes)
            tmp_path = tmp.name
        try:
            with open(tmp_path, "rb") as af:
                if _stt_provider == "groq":
                    resp = _stt_client.audio.transcriptions.create(
                        model="whisper-large-v3-turbo",
                        file=af,
                        response_format="text",
                        prompt="финансы расходы доходы сумма сум тысяч минг",
                    )
                    return (resp if isinstance(resp, str) else resp.text).strip()
                else:
                    resp = _stt_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=af,
                        prompt="финансы расходы доходы сумма сум тысяч минг",
                    )
                    return resp.text.strip()
        finally:
            try: os.unlink(tmp_path)
            except: pass
    try:
        return await loop.run_in_executor(None, _do)
    except Exception as e:
        log.warning(f"STT error ({_stt_provider}): {e}")
        return None

_VOICE_NUM_WORDS = {
    "один":1,"одна":1,"два":2,"две":2,"три":3,"четыре":4,"пять":5,
    "шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,
    "одиннадцать":11,"двенадцать":12,"тринадцать":13,"четырнадцать":14,"пятнадцать":15,
    "шестнадцать":16,"семнадцать":17,"восемнадцать":18,"девятнадцать":19,
    "двадцать":20,"тридцать":30,"сорок":40,"пятьдесят":50,"шестьдесят":60,
    "семьдесят":70,"восемьдесят":80,"девяносто":90,"сто":100,
    "двести":200,"триста":300,"четыреста":400,"пятьсот":500,
    "шестьсот":600,"семьсот":700,"восемьсот":800,"девятьсот":900,
    "тысяч":1000,"тысячу":1000,"тысячи":1000,"тысяча":1000,
    "миллион":1_000_000,"миллиона":1_000_000,"миллионов":1_000_000,
    "bir":1,"ikki":2,"uch":3,"besh":5,"olti":6,"yetti":7,"sakkiz":8,
    "o'n":10,"on":10,"yigirma":20,"o'ttiz":30,"qirq":40,"ellik":50,
    "yuz":100,"ming":1000,"mln":1_000_000,
}
_VOICE_MULTIPLIERS = {1000, 1_000_000}

def _parse_voice_amount(text: str) -> int | None:
    t = text.lower().strip()
    for pat, mult in [
        (r"(\d[\d\s]*)\s*тысяч[а-я]*", 1000),
        (r"(\d[\d\s]*)\s*минг", 1000),
        (r"(\d[\d\s]*)\s*ming", 1000),
        (r"(\d[\d\s]*)\s*миллион[а-я]*", 1_000_000),
        (r"(\d[\d\s]*)\s*mln", 1_000_000),
    ]:
        m = re.search(pat, t)
        if m:
            return int(m.group(1).replace(" ", "")) * mult
    digits = re.findall(r"\d+", t)
    if digits:
        return int("".join(digits))
    # word numbers
    words = re.findall(r"[a-zA-Zа-яёА-ЯЁ']+", t)
    total, cur = 0, 0
    for w in words:
        v = _VOICE_NUM_WORDS.get(w)
        if v is None: continue
        if v in _VOICE_MULTIPLIERS:
            cur = (cur or 1) * v; total += cur; cur = 0
        else:
            cur += v
    total += cur
    return total if total > 0 else None

_VOICE_CATS = [
    (["продукт","еда","food","oziq","supermarket","супермаркет","овощ","фрукт","бакалея"],     "🛒","Продукты"),
    (["кафе","ресторан","cafe","restoran","обед","lunch","ужин","завтрак","taom","кофе","coffee"], "☕","Кафе/Рестораны"),
    (["транспорт","такси","bus","автобус","метро","metro","yol","avtobus","taxi","uber"],       "🚌","Транспорт"),
    (["телефон","интернет","связь","telefon","internet","aloqa"],                              "📱","Связь"),
    (["аренда","квартира","дом","ijara","uy","rent"],                                         "🏠","Аренда"),
    (["здоровье","лекарство","аптека","dori","dorixona","врач"],                              "💊","Здоровье"),
    (["одежда","kiyim","обувь","магазин","shop"],                                             "👕","Одежда"),
    (["зарплата","salary","maosh","доход","income","kirim","daromad"],                        "💵","Зарплата"),
    (["развлечения","кино","cinema","oyin","развлечени"],                                     "🎉","Развлечения"),
    (["коммунал","свет","газ","вода","gaz","suv","kommunal"],                                "⚡","Коммуналка"),
]

def _parse_voice_category(text: str):
    t = text.lower()
    for kws, icon, name in _VOICE_CATS:
        if any(kw in t for kw in kws):
            return icon, name
    return "💬", "Прочее"

def _parse_voice_type(text: str) -> str:
    income_kws = ["доход","зарплата","получил","oldi","maosh","salary","income","daromad","kirim","пришло"]
    t = text.lower()
    return "income" if any(kw in t for kw in income_kws) else "expense"

# ── Helpers ───────────────────────────────────────────────────────────────────
def row_to_tx(r) -> dict:
    return {
        "id":                    r["id"],
        "type":                  r["type"],
        "cat":                   r["cat_icon"],
        "catName":               r["cat_name"],
        "note":                  r["note"],
        "amount":                r["amount"],
        "dateStr":               str(r["date_str"]),
        "time":                  r["time_str"],
        "currency":              r["currency"],
        "recurring":             r["recurring"],
        "account":               r["account"],
        "linkedGoalId":          r.get("linked_goal_id"),
        "linkedContributionId":  r.get("linked_contribution_id"),
        "originalAmount":        r.get("original_amount"),
        "originalCurrency":      r.get("original_currency"),
        "exchangeRate":          r.get("exchange_rate"),
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
        "account": r.get("account", "Наличка") if isinstance(r, dict) else (r["account"] if "account" in r.keys() else "Наличка"),
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
        SELECT COALESCE(SUM(amount),0) AS total FROM transactions
        WHERE user_id=%s AND type='expense' AND cat_name=%s
          AND date_str >= date_trunc('month', CURRENT_DATE)::date
    """, (user_id, budget_name))
    return int(cur.fetchone()["total"])

def _gen_ref_code(uid: int) -> str:
    """Generate a short unique referral code for a user."""
    return f"r{secrets.token_hex(4)}{uid % 1000:03d}"

def upsert_user(conn, tg_user: dict) -> tuple[dict, bool]:
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
        existing = cur.fetchone()
        if existing:
            # Ensure ref_code exists (backfill for old users)
            if not existing.get("ref_code"):
                rc = _gen_ref_code(uid)
                cur.execute("UPDATE users SET ref_code=%s WHERE id=%s AND ref_code IS NULL", (rc, uid))
                conn.commit()
                cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
                existing = cur.fetchone()
            return dict(existing), False
        # New user — insert with default categories
        ref_code = _gen_ref_code(uid)
        cur.execute("""
            INSERT INTO users (id, first_name, last_name, username, language, ref_code)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (uid, tg_user.get("first_name",""), tg_user.get("last_name",""),
              tg_user.get("username",""), tg_user.get("language_code","ru"), ref_code))
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
        cur.execute("SELECT streak, streak_record, last_log_date, xp, level, streak_restore_to FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        today = date.today()
        last = u["last_log_date"]
        streak = u["streak"]
        record = u["streak_record"]
        restore_to = u.get("streak_restore_to")
        xp_earned = 10  # base xp per transaction
        streak_continued = False

        if last == today:
            pass  # already logged today, no streak change
        elif restore_to and (last is None or last < today):
            # Admin offered a streak restore — user logged today → give it back (+1 for today)
            streak = restore_to + 1
            xp_earned += 20
            streak_continued = True
            cur.execute("UPDATE users SET streak_restore_to=NULL WHERE id=%s", (user_id,))
        elif last is None or last < today - timedelta(days=1):
            # Reset or start streak
            streak = 1
        elif last == today - timedelta(days=1):
            # Continued streak
            streak += 1
            xp_earned += 20  # streak bonus
            streak_continued = True

        new_record = max(record, streak)
        new_xp = u["xp"] + xp_earned
        new_level = max(1, new_xp // 500 + 1)

        cur.execute("""
            UPDATE users SET streak=%s, streak_record=%s, xp=%s, level=%s, last_log_date=%s
            WHERE id=%s
        """, (streak, new_record, new_xp, new_level, today, user_id))
        return streak, xp_earned, streak_continued

# ── Rank tiers: groups of 3 levels → a named rank ─────────────────────────────
RANK_TIERS = [
    {"icon": "🥉", "name": "Новичок"},
    {"icon": "🥈", "name": "Ученик"},
    {"icon": "🥇", "name": "Знаток"},
    {"icon": "💎", "name": "Эксперт"},
    {"icon": "👑", "name": "Мастер"},
    {"icon": "🔥", "name": "Легенда"},
]
def level_to_rank(level: int) -> dict:
    """Map a level to a rank tier. Levels 1-3 → tier 0, 4-6 → tier 1, etc.
    Caps at the last tier (Легенда) for high levels."""
    lvl = max(1, level or 1)
    idx = min((lvl - 1) // 3, len(RANK_TIERS) - 1)
    t = RANK_TIERS[idx]
    return {"tier": idx, "icon": t["icon"], "name": t["name"],
            "min_level": idx * 3 + 1,
            "max_level": (idx + 1) * 3 if idx < len(RANK_TIERS) - 1 else 9999}

def notify_achievements(user_id: int, new_ach_ids: list[str]):
    """Send one achievement notification — pick the highest XP one.
    If multiple earned at once, send only the best; skip the rest silently."""
    if not new_ach_ids:
        return
    # Pick the highest-XP achievement to avoid notification spam
    best_id = max(new_ach_ids, key=lambda aid: next(
        (a["xp"] for a in ALL_ACHIEVEMENTS if a["id"] == aid), 0))
    ach = next((a for a in ALL_ACHIEVEMENTS if a["id"] == best_id), None)
    if not ach:
        return
    kb = {"inline_keyboard": [[{"text": "🏆 Открыть Finly", "web_app": {"url": APP_URL + "?page=rewards"}}]]}
    try:
        send_tg_message_sync(user_id,
            f"{ach['icon']} <b>Новый значок!</b>\n\n"
            f"Вы получили значок «<b>{ach['name']}</b>»\n"
            f"<i>{ach['desc']}</i>\n\n"
            f"<b>+{ach['xp']} XP</b> начислено 🎉", kb)
    except Exception:
        pass

def add_xp_log(cur, user_id: int, icon: str, label: str, sub: str, pts: int):
    cur.execute("""
        INSERT INTO xp_log (user_id, icon, label, sub, pts, date_str)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (user_id, icon, label, sub, pts, date.today()))

def check_achievements(conn, user_id: int, extra_checks: dict = None) -> list[str]:
    """Check and award any newly unlocked achievements. Returns list of new ach IDs.
    extra_checks: optional dict of {ach_id: bool} for goal/debt/budget checks.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT ach_id FROM achievements WHERE user_id=%s", (user_id,))
        earned_ids = {r["ach_id"] for r in cur.fetchall()}
        cur.execute("SELECT streak, xp, last_log_date FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s", (user_id,))
        tx_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM personal_goals WHERE user_id=%s AND done=TRUE", (user_id,))
        goals_done = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM personal_debts WHERE user_id=%s AND paid=TRUE", (user_id,))
        debts_paid = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM friends
            WHERE (requester_id=%s OR addressee_id=%s) AND status='accepted'
        """, (user_id, user_id))
        friends_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM savings_jars WHERE user_id=%s", (user_id,))
        savings_count = cur.fetchone()["cnt"]

        # Extra counts for new achievements
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND source='voice'", (user_id,))
        voice_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND source='shortcut'", (user_id,))
        shortcut_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM categories WHERE user_id=%s", (user_id,))
        cat_count = cur.fetchone()["cnt"]
        cur.execute("SELECT budget_plan_active FROM users WHERE id=%s", (user_id,))
        has_budget = (cur.fetchone() or {}).get("budget_plan_active", False)

        # New checks for richer achievements
        cur.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE user_id=%s", (user_id,))
        feedback_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND EXTRACT(HOUR FROM created_at) < 9", (user_id,))
        early_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND EXTRACT(HOUR FROM created_at) < 6", (user_id,))
        night_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND amount >= 500000", (user_id,))
        big_tx_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(DISTINCT account) AS cnt FROM transactions WHERE user_id=%s", (user_id,))
        account_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND note IS NOT NULL AND note != '' AND note != cat_name", (user_id,))
        note_count = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s AND EXTRACT(DOW FROM created_at) = 0", (user_id,))
        weekend_count = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT COUNT(DISTINCT TO_CHAR(created_at,'YYYY-MM')) AS cnt
            FROM transactions WHERE user_id=%s
        """, (user_id,))
        active_months = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT COUNT(DISTINCT cat_name) AS cnt FROM transactions
            WHERE user_id=%s AND type='expense'
            AND date_str >= DATE_TRUNC('month', NOW())::date
        """, (user_id,))
        this_month_cats = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM personal_goals WHERE user_id=%s AND done=TRUE", (user_id,))
        goals10 = cur.fetchone()["cnt"]

        newly_earned = []
        checks = [
            # Transaction milestones — require real sustained effort
            ("first_tx",    tx_count >= 1),
            ("tx10",        tx_count >= 10),
            ("tx50",        tx_count >= 50),
            ("tx100",       tx_count >= 100),
            ("tx500",       tx_count >= 500),
            ("tx1000",      tx_count >= 1000),
            # Streak milestones — only meaningful ones, spaced far apart
            ("fire7",       u["streak"] >= 7),
            ("streak30",    u["streak"] >= 30),
            ("streak100",   u["streak"] >= 100),
            ("streak200",   u["streak"] >= 200),
            # Level milestones
            ("level5",      u["xp"] >= 2000),
            ("level10",     u["xp"] >= 4500),
            # Goals & debts — require real-world actions
            ("goal1",       goals_done >= 1),
            ("goal5",       goals_done >= 5),
            ("goal10",      goals10 >= 10),
            ("debt1",       debts_paid >= 1),
            ("debt5",       debts_paid >= 5),
            ("debt10",      debts_paid >= 10),
            # Social
            ("friend1",     friends_count >= 1),
            # Feature discovery — first-use rewards, not exploitable
            ("savings5",    savings_count >= 5),
            ("voice1",      voice_count >= 1),
            ("tx_voice5",   voice_count >= 5),
            ("finlyfast1",  shortcut_count >= 1),
            ("finlyfast10", shortcut_count >= 10),
            # Behavior patterns — raised thresholds to require real habits
            ("early_5",     early_count >= 10),     # 10 entries before 9am (was 5)
            ("night_5",     night_count >= 10),     # 10 entries after midnight (was 5)
            ("big_spender", big_tx_count >= 3),     # 3 transactions of 500k+ (was 1)
            ("account3",    account_count >= 5),    # 5 different accounts (was 3)
            ("cats_used5",  this_month_cats >= 8),  # 8 categories in a month (was 5)
            ("months3",     active_months >= 3),
            ("months6",     active_months >= 6),
            ("note20",      note_count >= 30),      # 30 notes (was 20)
        ]
        if extra_checks:
            for ach_id, cond in extra_checks.items():
                checks.append((ach_id, cond))
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
    amount:    int                   # always in UZS (converted at logging time)
    date_str:  str
    time_str:  str = "00:00"
    currency:  str = "UZS"
    recurring: bool = False
    account:   str = "Карта"
    linked_goal_id:          Optional[int] = None
    linked_contribution_id:  Optional[int] = None
    original_amount:   Optional[int] = None   # amount in original currency (e.g. 100 for $100)
    original_currency: Optional[str] = None   # 'USD','EUR', etc. None = UZS
    exchange_rate:     Optional[int] = None   # rate at time of logging (e.g. 12200)

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
    account:  str = "Наличка"

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
    notifs:         Optional[bool] = None
    weekly_report:  Optional[bool] = None
    nickname:       Optional[str] = None
    rank_visible:   Optional[str] = None  # "everyone" | "friends" | "nobody"
    notif_overtake: Optional[bool] = None  # notify when someone passes you in ranks

class NicknamePatch(BaseModel):
    nickname: str

class FriendRequest(BaseModel):
    addressee_nickname: str

class FriendAction(BaseModel):
    action: str  # "accept" | "reject" | "remove"

class SocialDebtCreate(BaseModel):
    counterpart_nickname: Optional[str] = None
    counterpart_id: Optional[int] = None
    amount: int
    due_date: Optional[str] = None
    note: str = ""
    debt_type: str = "borrowed"

class SharedGoalCreate(BaseModel):
    partner_nickname: Optional[str] = None   # legacy single partner
    partner_id: Optional[int] = None         # legacy single partner
    member_ids: List[int] = []               # multi-member
    member_nicknames: List[str] = []         # multi-member (by nickname)
    name: str
    emoji: str = "🎯"
    target: int
    note: str = ""
    deadline: Optional[str] = None

class NotifPrefBody(BaseModel):
    notif: Optional[dict] = None
    report: Optional[dict] = None

class PersonalGoalCreate(BaseModel):
    emoji: str = "🎯"
    name: str
    target: int = 0
    duration: int = 6
    start_year: Optional[int] = None
    start_month: Optional[int] = None
    deadline_year: Optional[int] = None
    deadline_month: Optional[int] = None

class PersonalGoalPatch(BaseModel):
    saved: Optional[int] = None
    done: Optional[bool] = None
    done_date: Optional[str] = None
    contributions: Optional[list] = None
    name: Optional[str] = None
    emoji: Optional[str] = None
    target: Optional[int] = None

class PersonalDebtCreate(BaseModel):
    name: str
    amount: int = 0
    type: str = "borrowed"
    note: str = ""
    due_date: Optional[str] = None

class PersonalDebtPatch(BaseModel):
    paid: Optional[bool] = None
    name: Optional[str] = None
    amount: Optional[int] = None
    note: Optional[str] = None
    due_date: Optional[str] = None

class SavingsJarCreate(BaseModel):
    name: str
    emoji: str = "🏺"
    balance: int = 0
    target: Optional[int] = None
    note: str = ""

class SavingsJarPatch(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    balance: Optional[int] = None
    target: Optional[int] = None
    note: Optional[str] = None

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run init_db in a background thread so FastAPI starts immediately.
    import threading as _threading
    def _init_bg():
        try:
            init_db()
        except Exception as e:
            log.error(f"init_db failed: {e} — DB-dependent routes will fail")
    _threading.Thread(target=_init_bg, daemon=True).start()
    # NOTE: We do NOT register a Telegram webhook here. bot.py owns all update
    # delivery via long-polling (telebot). Registering a webhook would race
    # against polling on every restart and cause 409 "terminated by other
    # getUpdates" errors, dropping business_connection/business_message updates.
    # The /bot/webhook endpoint below remains an INTERNAL forward target that
    # bot.py posts to from its handlers — it is not a real Telegram webhook.
    # Start background schedulers
    scheduler_task = asyncio.create_task(_reminder_scheduler())
    weekly_task    = asyncio.create_task(_weekly_scheduler())
    yield
    for t in (scheduler_task, weekly_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Finly API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Frontend ─────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))

# Serve React and other static JS from Railway (no CDN dependency)
app.mount("/static", StaticFiles(directory=_HERE), name="static")

_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

@app.get("/", include_in_schema=False)
def serve_app():
    return FileResponse(os.path.join(_HERE, "index.html"), media_type="text/html", headers=_NO_CACHE)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION, "ts": datetime.utcnow().isoformat()}

# ── Auth / bootstrap ──────────────────────────────────────────────────────────
# ── Native iOS app: link-code redemption ─────────────────────────────────────
@app.post("/api/link/redeem")
def link_redeem(body: dict, conn=Depends(db)):
    """Exchange a 6-digit /link code (from the bot) for a long-lived bearer token.
    No Telegram initData needed — this is how the native iOS app logs in."""
    import secrets as _secrets
    code = (body.get("code") or "").strip().replace(" ", "")
    if not code:
        raise HTTPException(400, "Code required")
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM link_codes WHERE code=%s", (code,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Неверный код")
        if row["expires_at"] < datetime.now(row["expires_at"].tzinfo):
            cur.execute("DELETE FROM link_codes WHERE code=%s", (code,))
            conn.commit()
            raise HTTPException(410, "Код истёк — запросите новый через /link")
        uid = row["user_id"]
        token = _secrets.token_urlsafe(32)
        cur.execute("INSERT INTO device_tokens (token, user_id, platform) VALUES (%s,%s,'ios')", (token, uid))
        cur.execute("DELETE FROM link_codes WHERE code=%s", (code,))  # one-time use
        cur.execute("SELECT first_name, nickname FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
    conn.commit()
    return {"token": token, "user_id": uid,
            "first_name": u.get("first_name", "") if u else "",
            "nickname": u.get("nickname", "") if u else ""}

@app.post("/api/link/logout")
def link_logout(authorization: str = Header(default=""), conn=Depends(db)):
    """Revoke the current device token (native app sign-out)."""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM device_tokens WHERE token=%s", (token,))
        conn.commit()
    return {"ok": True}

@app.post("/api/admin/offer_streak_restore")
def admin_offer_streak_restore(user_id: int, token: str = "", send_message: bool = True, conn=Depends(db)):
    """Restore a user's streak IMMEDIATELY to their best value.
    set send_message=false to restore silently without a bot DM."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("SELECT streak, streak_record, first_name FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        restore_val = max(u.get("streak_record") or 0, u.get("streak") or 0)
        if restore_val < 1:
            restore_val = 1
        yesterday = date.today() - timedelta(days=1)
        cur.execute("""
            UPDATE users SET streak=%s,
                   streak_record=GREATEST(streak_record, %s),
                   last_log_date=%s,
                   streak_restore_to=NULL
            WHERE id=%s
        """, (restore_val, restore_val, yesterday, user_id))
    conn.commit()
    if send_message:
        name = (u.get("first_name") or "").split()[0] if u.get("first_name") else ""
        kb = {"inline_keyboard": [[{"text": "🔥 Открыть Finly", "web_app": {"url": APP_URL + "?page=add"}}]]}
        send_tg_message_sync(user_id,
            f"🔥 <b>{name}, твоя серия восстановлена!</b>\n\n"
            f"Мы вернули тебе серию <b>{restore_val} дней</b> 🎉\n\n"
            f"Запиши трату сегодня, чтобы продолжить её и не потерять снова 👇", kb)
    return {"ok": True, "restored_to": restore_val}

@app.get("/api/admin/feedback")
def admin_feedback(token: str = "", conn=Depends(db)):
    """List feedback WITH sender identity (anonymous to users, attributed for admin)."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.id, f.text, f.rating, f.source, f.created_at, f.resolved,
                   u.id AS user_id, u.first_name, u.username, u.nickname
            FROM feedback f JOIN users u ON u.id = f.user_id
            ORDER BY f.created_at DESC LIMIT 300
        """)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
    return rows

@app.post("/api/admin/feedback/{fb_id}/resolve")
def admin_resolve_feedback(fb_id: int, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("UPDATE feedback SET resolved=NOT resolved WHERE id=%s RETURNING resolved", (fb_id,))
        row = cur.fetchone()
    conn.commit()
    return {"resolved": row["resolved"] if row else False}

@app.post("/api/admin/feedback/{fb_id}/reply")
def admin_reply_feedback(fb_id: int, body: dict, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "Message is empty")
    with conn.cursor() as cur:
        cur.execute("SELECT f.user_id, u.first_name FROM feedback f JOIN users u ON u.id=f.user_id WHERE f.id=%s", (fb_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Feedback not found")
    kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}}]]}
    send_tg_message_sync(row["user_id"],
        f"💬 <b>Ответ от команды Finly</b>\n\n{msg}", kb)
    return {"ok": True}

@app.post("/api/admin/send_feedback_blast")
def admin_send_feedback_blast(token: str = "", conn=Depends(db)):
    """Send a feedback request to all users: active get 'what do you love?', inactive get 'why aren't you using it?'
    Returns counts of messages sent."""
    _admin_auth(token)
    today = date.today()
    cutoff = today - timedelta(days=14)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, first_name, last_log_date
            FROM users WHERE nickname IS NOT NULL AND nickname != ''
        """)
        users = cur.fetchall()
    kb = {"inline_keyboard": [[{"text": "💜 Оставить отзыв", "web_app": {"url": APP_URL + "?page=feedback"}}]]}
    sent_active = sent_inactive = 0
    for u in users:
        uid = u["id"]
        last = u.get("last_log_date")
        name = (u.get("first_name") or "").split()[0] or "друг"
        if last and last >= cutoff:
            msg = (
                f"💜 <b>{name}, нам важно твоё мнение!</b>\n\n"
                f"Ты уже {(today - last).days if last else '?'} дней пользуешься Finly.\n"
                f"Что тебе особенно нравится? Что добавить или убрать?\n\n"
                f"Оставь отзыв — мы читаем каждое сообщение 🙏\n"
                f"Тебе начислят <b>+100 XP</b> за отзыв! 🎁"
            )
            sent_active += 1
        else:
            msg = (
                f"👋 <b>{name}, скучаем по тебе!</b>\n\n"
                f"Давно не видели тебя в Finly. Расскажи — что мешает вести записи?\n"
                f"Что добавить, чтобы приложение стало удобнее?\n\n"
                f"Оставь отзыв — мы читаем каждое сообщение 🙏\n"
                f"Тебе начислят <b>+100 XP</b> за отзыв! 🎁"
            )
            sent_inactive += 1
        try:
            send_tg_message_sync(uid, msg, kb)
        except Exception:
            pass
    return {"ok": True, "sent_active": sent_active, "sent_inactive": sent_inactive}

@app.post("/api/admin/mint_link_code")
def admin_mint_link_code(user_id: int, token: str = "", conn=Depends(db)):
    """Admin: mint a link code for a user (support tool + iOS testing)."""
    _admin_auth(token)
    import random as _random
    code = f"{_random.randint(0, 999999):06d}"
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id=%s", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "User not found")
        cur.execute("DELETE FROM link_codes WHERE user_id=%s", (user_id,))
        cur.execute("INSERT INTO link_codes (code, user_id, expires_at) VALUES (%s,%s, NOW() + INTERVAL '15 minutes')",
                    (code, user_id))
    conn.commit()
    return {"code": code, "user_id": user_id, "expires_in_min": 15}

@app.post("/api/auth")
def auth(tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Called on webapp open. Returns full user state in one shot."""
    user, is_new = upsert_user(conn, tg_user)
    uid = user["id"]

    # Referral: handle invite link — record referred_by and award XP to referrer
    if is_new:
        start_param = tg_user.get("__start_param", "")
        # Support both old format "ref_12345" (user_id) and new "ref_<code>" (ref_code lookup)
        if start_param.startswith("ref_"):
            try:
                ref_part = start_param[4:]
                ref_uid = None
                with conn.cursor() as cur:
                    # Try numeric user_id first (legacy)
                    try:
                        maybe_id = int(ref_part)
                        cur.execute("SELECT id FROM users WHERE id=%s", (maybe_id,))
                        row = cur.fetchone()
                        if row:
                            ref_uid = row["id"]
                    except ValueError:
                        pass
                    # Try ref_code lookup
                    if ref_uid is None:
                        cur.execute("SELECT id FROM users WHERE ref_code=%s", (ref_part,))
                        row = cur.fetchone()
                        if row:
                            ref_uid = row["id"]

                if ref_uid and ref_uid != uid:
                    with conn.cursor() as cur:
                        # Record who referred this new user
                        cur.execute("UPDATE users SET referred_by=%s WHERE id=%s", (ref_uid, uid))
                        # Award XP to referrer
                        cur.execute("UPDATE users SET xp=xp+100 WHERE id=%s RETURNING xp", (ref_uid,))
                        new_xp = cur.fetchone()["xp"]
                        cur.execute("UPDATE users SET level=GREATEST(level,%s) WHERE id=%s",
                                    (max(1, new_xp // 500 + 1), ref_uid))
                        add_xp_log(cur, ref_uid, "🤝", "Реферал", "Друг присоединился по вашей ссылке", 100)
                        # Check referral achievements
                        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE referred_by=%s", (ref_uid,))
                        ref_count = cur.fetchone()["cnt"]
                        cur.execute("SELECT ach_id FROM achievements WHERE user_id=%s AND ach_id IN ('referral1','referral5')", (ref_uid,))
                        earned = {r["ach_id"] for r in cur.fetchall()}
                        if ref_count >= 1 and "referral1" not in earned:
                            cur.execute("INSERT INTO achievements(user_id,ach_id) VALUES(%s,'referral1') ON CONFLICT DO NOTHING", (ref_uid,))
                            cur.execute("UPDATE users SET xp=xp+100 WHERE id=%s", (ref_uid,))
                            add_xp_log(cur, ref_uid, "🤝", "Значок: Пригласил", "Первый приглашённый друг", 100)
                        if ref_count >= 5 and "referral5" not in earned:
                            cur.execute("INSERT INTO achievements(user_id,ach_id) VALUES(%s,'referral5') ON CONFLICT DO NOTHING", (ref_uid,))
                            cur.execute("UPDATE users SET xp=xp+300 WHERE id=%s", (ref_uid,))
                            add_xp_log(cur, ref_uid, "🌟", "Значок: Амбассадор", "5 приглашённых друзей", 300)
                    conn.commit()
                    log.info(f"Referral: new user {uid} from referrer {ref_uid}")
            except Exception as e:
                log.warning(f"Referral handling failed: {e}")

    # Lazy streak reset: if user hasn't logged in >1 day, streak expired
    if user.get("last_log_date"):
        days_since = (date.today() - user["last_log_date"]).days
        if days_since > 1 and user.get("streak", 0) > 0:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET streak=0 WHERE id=%s RETURNING streak", (uid,))
            conn.commit()
            user = dict(user)
            user["streak"] = 0

    with conn.cursor() as cur:
        # true balance (all transactions, not just last 200)
        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE -amount END), 0) AS balance
            FROM transactions WHERE user_id=%s
        """, (uid,))
        true_balance = int(cur.fetchone()["balance"])

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
        exp_cats = [{"id":r["id"],"icon":r["icon"],"name":r["name"]} for r in cats_raw if r["type"]=="expense"]
        inc_cats = [{"id":r["id"],"icon":r["icon"],"name":r["name"]} for r in cats_raw if r["type"]=="income"]

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
            "nickname":     user["nickname"] or "",
            "streak":       user["streak"],
            "streak_record":user["streak_record"],
            "xp":           user["xp"],
            "level":        user["level"],
            "notifs":       user["notifs"],
            "weekly_report":user["weekly_report"],
            "last_log_date":str(user["last_log_date"]) if user["last_log_date"] else None,
            "rank_visible": user.get("rank_visible", "everyone"),
            "notif_overtake": user.get("notif_overtake", True),
            "ref_code":     user.get("ref_code") or "",
        },
        "txs":          txs,
        "drafts":       drafts,
        "budgets":      budgets,
        "exp_cats":     exp_cats,
        "inc_cats":     inc_cats,
        "achievements": achievements,
        "xp_log":       xp_log,
        "is_new":          is_new,
        # needs_onboarding = True when user has no nickname set.
        # This is the authoritative server-side signal — never trust localStorage alone.
        # Covers: direct new users, referral users created by bot before app opens,
        # and any user who somehow never set a nickname.
        "needs_onboarding": not bool(user.get("nickname")),
        "balance":          true_balance,
    }

def _ach_progress(ach_id, user, tx_count):
    streak = user.get("streak") or 0
    m = {
        "fire7":      (streak, 7),
        "fire21":     (streak, 21),
        "first_tx":   (tx_count, 1),
        "tx10":       (tx_count, 10),
        "tx50":       (tx_count, 50),
        "tx100":      (tx_count, 100),
        "tx500":      (tx_count, 500),
        "streak3":    (streak, 3),
        "streak14":   (streak, 14),
        "streak30":   (streak, 30),
        "streak60":   (streak, 60),
        "streak100":  (streak, 100),
    }
    if ach_id in m:
        v, t = m[ach_id]
        return min(100, round(v / t * 100)) if t else 0
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
async def create_transaction(body: TxCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,recurring,account,linked_goal_id,linked_contribution_id,original_amount,original_currency,exchange_rate)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.type, body.cat_icon, body.cat_name, body.note,
              body.amount, body.date_str, body.time_str, body.currency,
              body.recurring, body.account,
              body.linked_goal_id, body.linked_contribution_id,
              body.original_amount, body.original_currency, body.exchange_rate))
        tx = row_to_tx(cur.fetchone())

    # Streak + XP
    with conn.cursor() as cur:
        cur.execute("SELECT xp FROM users WHERE id=%s", (uid,))
        old_xp = (cur.fetchone() or {}).get("xp", 0)

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

    # Async fire-and-forget notifications
    try:
        if body.type == "expense":
            asyncio.create_task(_check_budget_alerts(uid))
        pass  # overtake notifications removed
        if newly_earned:
            import threading as _t
            _t.Thread(target=notify_achievements, args=(uid, newly_earned), daemon=True).start()
    except Exception:
        pass  # background tasks are non-critical; don't break the response

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
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM transactions WHERE id=%s AND user_id=%s", (tx_id, uid))
        old = cur.fetchone()
        if not old:
            raise HTTPException(404, "Transaction not found")
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
        cur.execute(f"UPDATE transactions SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Transaction not found")
        tx = row_to_tx(row)
        # Sync linked goal contribution amount if amount changed
        if body.amount is not None and old.get("linked_contribution_id") and old.get("linked_goal_id"):
            old_amt = old["amount"]
            new_amt = body.amount
            delta = new_amt - old_amt
            if delta != 0:
                cur.execute("UPDATE shared_goal_contributions SET amount=%s WHERE id=%s",
                            (new_amt, old["linked_contribution_id"]))
                cur.execute(
                    "UPDATE shared_goals SET saved=GREATEST(0,saved+%s) WHERE id=%s AND status!='completed'",
                    (delta, old["linked_goal_id"])
                )
    conn.commit()
    return tx

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM transactions WHERE id=%s AND user_id=%s", (tx_id, uid))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Transaction not found")
        # If linked to a shared goal contribution, reverse the saved amount
        if row.get("linked_contribution_id") and row.get("linked_goal_id"):
            cur.execute("DELETE FROM shared_goal_contributions WHERE id=%s RETURNING amount",
                        (row["linked_contribution_id"],))
            contrib = cur.fetchone()
            if contrib:
                cur.execute(
                    "UPDATE shared_goals SET saved=GREATEST(0,saved-%s) WHERE id=%s AND status!='completed'",
                    (contrib["amount"], row["linked_goal_id"])
                )
        cur.execute("DELETE FROM transactions WHERE id=%s AND user_id=%s RETURNING id", (tx_id, uid))
        cur.fetchone()
    conn.commit()
    return {"deleted": tx_id}

# ── Drafts ────────────────────────────────────────────────────────────────────
@app.post("/api/drafts")
def create_draft(body: DraftCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO drafts (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,account)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.type, body.cat_icon, body.cat_name, body.note,
              body.amount, body.date_str, body.time_str, body.currency, body.account))
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
        cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 AS next_order FROM categories WHERE user_id=%s AND type=%s", (uid, body.type))
        order = (cur.fetchone() or {}).get("next_order", 1)
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

class CategoryReset(BaseModel):
    type: str  # "expense" | "income"

@app.post("/api/categories/reset")
def reset_categories(body: CategoryReset, tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Delete all user categories of a type and restore the built-in defaults."""
    uid = tg_user["id"]
    if body.type not in ("expense", "income"):
        raise HTTPException(400, "type must be expense or income")
    defaults = DEFAULT_EXP_CATS if body.type == "expense" else DEFAULT_INC_CATS
    with conn.cursor() as cur:
        cur.execute("DELETE FROM categories WHERE user_id=%s AND type=%s", (uid, body.type))
        for i, (icon, name) in enumerate(defaults):
            cur.execute(
                "INSERT INTO categories(user_id,type,icon,name,sort_order) VALUES(%s,%s,%s,%s,%s)",
                (uid, body.type, icon, name, i)
            )
        cur.execute(
            "SELECT id,icon,name FROM categories WHERE user_id=%s AND type=%s ORDER BY sort_order",
            (uid, body.type)
        )
        cats = [{"id": r["id"], "icon": r["icon"], "name": r["name"]} for r in cur.fetchall()]
    conn.commit()
    return cats

# ── User settings ─────────────────────────────────────────────────────────────
@app.patch("/api/user")
def patch_user(body: UserPatch, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    if body.notifs is not None:
        fields.append("notifs=%s"); vals.append(body.notifs)
    if body.weekly_report is not None:
        fields.append("weekly_report=%s"); vals.append(body.weekly_report)
    send_guide_after = False
    if body.nickname is not None:
        nick = body.nickname.strip().lower()[:32]
        # Check uniqueness + detect first-time set (onboarding completion)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE nickname=%s AND id!=%s", (nick, uid))
            if cur.fetchone():
                raise HTTPException(409, "Nickname already taken")
            cur.execute("SELECT nickname FROM users WHERE id=%s", (uid,))
            prev = cur.fetchone()
            # First time setting a nickname → user just finished onboarding
            if prev and not (prev.get("nickname") or "").strip():
                send_guide_after = True
        fields.append("nickname=%s"); vals.append(nick)
    if body.rank_visible is not None:
        if body.rank_visible not in ("everyone", "friends", "nobody", "anonymous"):
            raise HTTPException(400, "rank_visible must be everyone/friends/nobody/anonymous")
        fields.append("rank_visible=%s"); vals.append(body.rank_visible)
    if body.notif_overtake is not None:
        fields.append("notif_overtake=%s"); vals.append(body.notif_overtake)
    if not fields:
        raise HTTPException(400, "No fields to update")
    vals.append(uid)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE users SET {','.join(fields)} WHERE id=%s RETURNING *", vals)
        u = cur.fetchone()
    conn.commit()
    # Fire the one-time feature guide after onboarding completes (nickname first set)
    if send_guide_after:
        try:
            send_feature_guide(uid)
        except Exception as e:
            log.warning(f"feature guide send failed for {uid}: {e}")
    return {"notifs": u["notifs"], "weekly_report": u["weekly_report"],
            "rank_visible": u["rank_visible"], "notif_overtake": u["notif_overtake"]}

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.get("/api/leaderboard")
def leaderboard(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        # Friend IDs for "friends-only" visibility check
        cur.execute("""
            SELECT CASE WHEN requester_id=%s THEN addressee_id ELSE requester_id END AS friend_id
            FROM friends WHERE (requester_id=%s OR addressee_id=%s) AND status='accepted'
        """, (uid, uid, uid))
        friend_ids = {r["friend_id"] for r in cur.fetchall()}

        cur.execute("""
            SELECT id, nickname, xp, level, streak, rank_visible
            FROM users WHERE nickname IS NOT NULL AND nickname != ''
            ORDER BY xp DESC LIMIT 100
        """)
        rows = cur.fetchall()

    result = []
    rank = 0
    me_in_list = False
    for r in rows:
        is_you = r["id"] == uid
        rv = (r.get("rank_visible") or "everyone")
        is_anon = rv in ("nobody", "anonymous") and not is_you
        is_friends_only = rv == "friends" and r["id"] not in friend_ids and not is_you
        # Hidden users still count toward rank number but appear as Аноним
        rank += 1
        if rank > 50 and not is_you:
            continue
        if is_you:
            me_in_list = True
        result.append({
            "rank":     rank,
            "id":       r["id"],
            "name":     "Аноним" if (is_anon or is_friends_only) else r["nickname"],
            "nickname": "" if (is_anon or is_friends_only) else r["nickname"],
            "xp":       r["xp"],
            "level":    r["level"],
            "streak":   r["streak"],
            "is_you":   is_you,
            "is_anon":  is_anon or is_friends_only,
        })

    # Append current user if outside top 50 (and has a nickname)
    if not me_in_list:
        with conn.cursor() as cur:
            cur.execute("SELECT id, nickname, xp, level, streak, rank_visible FROM users WHERE id=%s", (uid,))
            me = cur.fetchone()
            if me and me.get("nickname"):
                cur.execute("SELECT COUNT(*)+1 AS rank FROM users WHERE xp > %s AND nickname IS NOT NULL AND nickname != ''", (me["xp"],))
                my_rank = cur.fetchone()["rank"]
                result.append({
                    "rank": my_rank, "id": me["id"],
                    "name": me["nickname"], "nickname": me["nickname"],
                    "xp": me["xp"], "level": me["level"], "streak": me["streak"],
                    "is_you": True, "is_anon": False,
                })
    return result

# ── Feedback (anonymous to public, attributed in admin) ───────────────────────
@app.post("/api/feedback")
def submit_feedback(body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    text = (body.get("text") or "").strip()[:2000]
    rating = body.get("rating")
    source = body.get("source")
    if rating is not None:
        rating = max(1, min(5, int(rating)))
    if not text and not rating:
        raise HTTPException(400, "Текст отзыва пуст")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE user_id=%s", (uid,))
        prev_count = cur.fetchone()["cnt"]
        if source == "blast":
            xp_pts = 100
        elif prev_count == 0:
            xp_pts = 50  # first ever feedback
        else:
            xp_pts = 5   # subsequent feedbacks
        cur.execute(
            "INSERT INTO feedback (user_id, text, rating, source) VALUES (%s,%s,%s,%s)",
            (uid, text or "", rating, source))
        cur.execute("UPDATE users SET xp=xp+%s WHERE id=%s", (xp_pts, uid))
        add_xp_log(cur, uid, "💬", "Отзыв оставлен", "Спасибо за отзыв!", xp_pts)
    conn.commit()
    return {"ok": True, "xp": xp_pts}

# ── Nickname & user search ─────────────────────────────────────────────────────

@app.get("/api/users/search")
def search_users(q: str, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    q_lower = q.strip().lower()
    if len(q_lower) < 2:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, first_name, username, nickname, xp, level
            FROM users
            WHERE id != %s AND (
                LOWER(nickname) LIKE %s OR
                LOWER(username) LIKE %s OR
                LOWER(first_name) LIKE %s
            )
            ORDER BY xp DESC LIMIT 10
        """, (uid, f"%{q_lower}%", f"%{q_lower}%", f"%{q_lower}%"))
        rows = cur.fetchall()
    return [{"id": r["id"], "name": r["first_name"] or r["username"] or "User",
             "nickname": r["nickname"], "username": r["username"],
             "xp": r["xp"], "level": r["level"]} for r in rows]

# ── Friends ────────────────────────────────────────────────────────────────────

@app.post("/api/friends")
async def send_friend_request(body: FriendRequest, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    nick = body.addressee_nickname.strip().lower()
    with conn.cursor() as cur:
        cur.execute("SELECT id, first_name FROM users WHERE LOWER(nickname)=%s OR LOWER(username)=%s", (nick, nick))
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if target["id"] == uid:
            raise HTTPException(400, "Cannot add yourself")
        # Check existing
        cur.execute("SELECT status FROM friends WHERE requester_id=%s AND addressee_id=%s", (uid, target["id"]))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(409, f"Request already {existing['status']}")
        cur.execute(
            "INSERT INTO friends (requester_id, addressee_id) VALUES (%s,%s) RETURNING id",
            (uid, target["id"])
        )
    conn.commit()
    # Notify target with inline approve/reject buttons
    requester_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Принять",  "callback_data": f"friend:accept:{uid}"},
        {"text": "❌ Отклонить","callback_data": f"friend:reject:{uid}"},
    ]]}
    await send_tg_message(
        target["id"],
        f"👋 <b>{requester_name}</b> хочет добавить вас в друзья в Finly!\n\nПринять запрос? 👇",
        keyboard
    )
    return {"ok": True, "addressee_id": target["id"]}

@app.get("/api/friends")
def get_friends(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        # My accepted friends (both directions)
        cur.execute("""
            SELECT u.id, u.first_name, u.username, u.nickname, u.xp, u.level, u.streak,
                   'accepted' as status, 'friend' as role
            FROM friends f
            JOIN users u ON (
                CASE WHEN f.requester_id=%s THEN f.addressee_id ELSE f.requester_id END = u.id
            )
            WHERE (f.requester_id=%s OR f.addressee_id=%s) AND f.status='accepted'
        """, (uid, uid, uid))
        friends = cur.fetchall()
        # Pending incoming
        cur.execute("""
            SELECT u.id, u.first_name, u.username, u.nickname, u.xp, u.level, u.streak,
                   'pending' as status, 'incoming' as role
            FROM friends f JOIN users u ON f.requester_id=u.id
            WHERE f.addressee_id=%s AND f.status='pending'
        """, (uid,))
        incoming = cur.fetchall()
        # Pending outgoing
        cur.execute("""
            SELECT u.id, u.first_name, u.username, u.nickname, u.xp, u.level, u.streak,
                   'pending' as status, 'outgoing' as role
            FROM friends f JOIN users u ON f.addressee_id=u.id
            WHERE f.requester_id=%s AND f.status='pending'
        """, (uid,))
        outgoing = cur.fetchall()
    def row(r, role):
        return {"id": r["id"], "name": r["first_name"] or r["username"] or "User",
                "nickname": r["nickname"], "username": r["username"],
                "xp": r["xp"], "level": r["level"], "streak": r["streak"],
                "status": r["status"], "role": role}
    return {
        "friends":  [row(r, "friend")   for r in friends],
        "incoming": [row(r, "incoming") for r in incoming],
        "outgoing": [row(r, "outgoing") for r in outgoing],
    }

@app.patch("/api/friends/{friend_id}")
async def update_friend(friend_id: int, body: FriendAction, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        if body.action == "accept":
            cur.execute("""
                UPDATE friends SET status='accepted'
                WHERE addressee_id=%s AND requester_id=%s AND status='pending'
                RETURNING requester_id
            """, (uid, friend_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Request not found")
        elif body.action in ("reject", "remove"):
            cur.execute("""
                DELETE FROM friends
                WHERE (requester_id=%s AND addressee_id=%s)
                   OR (requester_id=%s AND addressee_id=%s)
            """, (uid, friend_id, friend_id, uid))
        else:
            raise HTTPException(400, "Invalid action")
    conn.commit()
    if body.action == "accept":
        me_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"
        await send_tg_message(friend_id, f"✅ <b>{me_name}</b> принял ваш запрос в друзья в Finly!")
    return {"ok": True}

# ── Social Debts ───────────────────────────────────────────────────────────────

@app.post("/api/social_debts")
async def create_social_debt(body: SocialDebtCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    counterpart_id = body.counterpart_id
    counterpart_row = None
    with conn.cursor() as cur:
        if not counterpart_id and body.counterpart_nickname:
            nick = body.counterpart_nickname.strip().lower()
            cur.execute("SELECT id, first_name FROM users WHERE LOWER(nickname)=%s OR LOWER(username)=%s", (nick, nick))
            counterpart_row = cur.fetchone()
            if counterpart_row:
                counterpart_id = counterpart_row["id"]
        elif counterpart_id:
            cur.execute("SELECT id, first_name FROM users WHERE id=%s", (counterpart_id,))
            counterpart_row = cur.fetchone()
        cur.execute("""
            INSERT INTO social_debts (creator_id, counterpart_id, amount, due_date, note, debt_type, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (uid, counterpart_id, body.amount,
              body.due_date or None, body.note, body.debt_type,
              "pending" if counterpart_id else "approved"))
        new_id = cur.fetchone()["id"]
    conn.commit()
    if counterpart_id and counterpart_row:
        creator_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"
        debt_word = "одолжил вам" if body.debt_type == "lent" else "взял у вас"
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Подтвердить", "callback_data": f"debt:approve:{new_id}"},
            {"text": "❌ Отклонить",   "callback_data": f"debt:reject:{new_id}"},
        ]]}
        await send_tg_message(
            counterpart_id,
            f"💸 <b>{creator_name}</b> {debt_word} <b>{body.amount:,} сум</b>\n"
            f"{'📅 Срок: ' + str(body.due_date) if body.due_date else ''}\n"
            f"{'📝 ' + body.note if body.note else ''}\n\n"
            f"Подтвердите долг 👇",
            keyboard
        )
    return {"id": new_id, "ok": True}

@app.get("/api/social_debts")
def get_social_debts(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sd.*,
                   cr.first_name AS creator_name, cr.nickname AS creator_nick, cr.username AS creator_uname,
                   cp.first_name AS cp_name, cp.nickname AS cp_nick, cp.username AS cp_uname
            FROM social_debts sd
            LEFT JOIN users cr ON sd.creator_id = cr.id
            LEFT JOIN users cp ON sd.counterpart_id = cp.id
            WHERE sd.creator_id=%s OR sd.counterpart_id=%s
            ORDER BY sd.created_at DESC
        """, (uid, uid))
        rows = cur.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "creator_id": r["creator_id"],
            "is_mine": r["creator_id"] == uid,
            "creator": {"name": r["creator_name"], "nickname": r["creator_nick"], "username": r["creator_uname"]},
            "counterpart": {"name": r["cp_name"], "nickname": r["cp_nick"], "username": r["cp_uname"]} if r["counterpart_id"] else None,
            "amount": r["amount"],
            "due_date": str(r["due_date"]) if r["due_date"] else None,
            "note": r["note"],
            "debt_type": r["debt_type"],
            "status": r["status"],
        })
    return result

@app.patch("/api/social_debts/{debt_id}")
async def update_social_debt(debt_id: int, body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    action = body.get("action")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM social_debts WHERE id=%s AND (counterpart_id=%s OR creator_id=%s)", (debt_id, uid, uid))
        debt = cur.fetchone()
        if not debt:
            raise HTTPException(404, "Debt not found")
        if action == "approve" and debt["counterpart_id"] == uid:
            cur.execute("UPDATE social_debts SET status='approved' WHERE id=%s", (debt_id,))
        elif action == "reject" and debt["counterpart_id"] == uid:
            cur.execute("UPDATE social_debts SET status='rejected' WHERE id=%s", (debt_id,))
        elif action == "cancel" and debt["creator_id"] == uid and debt["status"] == "pending":
            cur.execute("UPDATE social_debts SET status='rejected' WHERE id=%s", (debt_id,))
        elif action == "settle" and (debt["creator_id"] == uid or debt["counterpart_id"] == uid):
            cur.execute("UPDATE social_debts SET status='settled' WHERE id=%s", (debt_id,))
        else:
            raise HTTPException(400, "Invalid action or not authorized")
    conn.commit()
    me_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"
    if action == "approve":
        await send_tg_message(debt["creator_id"], f"✅ <b>{me_name}</b> подтвердил долг в Finly!")
    elif action == "reject":
        await send_tg_message(debt["creator_id"], f"❌ <b>{me_name}</b> отклонил долг в Finly.")
    elif action == "cancel":
        if debt["counterpart_id"]:
            await send_tg_message(debt["counterpart_id"], f"ℹ️ <b>{me_name}</b> отменил запрос долга.")
    return {"ok": True}

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

# ── Notification Prefs ────────────────────────────────────────────────────────
@app.post("/api/notif_prefs")
def save_notif_prefs(body: NotifPrefBody, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    fields, vals = [], []
    if body.notif is not None:
        fields.append("notif_pref_json=%s"); vals.append(json.dumps(body.notif))
    if body.report is not None:
        fields.append("report_pref_json=%s"); vals.append(json.dumps(body.report))
    if fields:
        vals.append(uid)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {','.join(fields)} WHERE id=%s", vals)
        conn.commit()
    return {"ok": True}

@app.get("/api/notif_prefs")
def get_notif_prefs(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT notif_pref_json, report_pref_json FROM users WHERE id=%s", (uid,))
        r = cur.fetchone()
    if not r:
        return {"notif": {}, "report": {}}
    try: notif = json.loads(r["notif_pref_json"] or "{}")
    except: notif = {}
    try: report = json.loads(r["report_pref_json"] or "{}")
    except: report = {}
    return {"notif": notif, "report": report}

# ── Budget Plan tracking ──────────────────────────────────────────────────────
@app.post("/api/budget_plan")
def set_budget_plan(body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Record budget plan activation or deactivation."""
    uid     = tg_user["id"]
    active  = body.get("active", False)
    days    = body.get("days")
    daily   = body.get("daily")
    with conn.cursor() as cur:
        if active:
            cur.execute("""
                UPDATE users SET budget_plan_active=TRUE, budget_plan_days=%s,
                    budget_plan_daily=%s, budget_plan_activated_at=NOW()
                WHERE id=%s
            """, (days, daily, uid))
        else:
            cur.execute("""
                UPDATE users SET budget_plan_active=FALSE, budget_plan_days=NULL,
                    budget_plan_daily=NULL, budget_plan_activated_at=NULL
                WHERE id=%s
            """, (uid,))
    conn.commit()
    return {"ok": True}

# ── Shared Goals ──────────────────────────────────────────────────────────────
@app.post("/api/shared_goals")
async def create_shared_goal(body: SharedGoalCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    creator_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"

    # Collect all member IDs (deduplicated, excluding creator)
    member_rows = []  # list of (id, first_name)
    with conn.cursor() as cur:
        # Legacy single partner support
        partner_id = body.partner_id
        if not partner_id and body.partner_nickname:
            nick = body.partner_nickname.strip().lower()
            cur.execute("SELECT id, first_name FROM users WHERE LOWER(nickname)=%s OR LOWER(username)=%s", (nick, nick))
            r = cur.fetchone()
            if r:
                partner_id = r["id"]

        # Collect IDs from member_ids
        seen = {uid}
        if partner_id and partner_id not in seen:
            cur.execute("SELECT id, first_name FROM users WHERE id=%s", (partner_id,))
            r = cur.fetchone()
            if r:
                member_rows.append(r)
                seen.add(partner_id)

        for mid in body.member_ids:
            if mid not in seen:
                cur.execute("SELECT id, first_name FROM users WHERE id=%s", (mid,))
                r = cur.fetchone()
                if r:
                    member_rows.append(r)
                    seen.add(mid)

        for nick in body.member_nicknames:
            n = nick.strip().lower()
            if not n:
                continue
            cur.execute("SELECT id, first_name FROM users WHERE LOWER(nickname)=%s OR LOWER(username)=%s", (n, n))
            r = cur.fetchone()
            if r and r["id"] not in seen:
                member_rows.append(r)
                seen.add(r["id"])

        # goal status: pending if there are members, active if solo
        status = "pending" if member_rows else "active"
        # keep partner_id for backward compat (first partner)
        first_partner_id = member_rows[0]["id"] if member_rows else None

        cur.execute("""
            INSERT INTO shared_goals (creator_id, partner_id, name, emoji, target, note, deadline, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (uid, first_partner_id, body.name, body.emoji, body.target, body.note,
              body.deadline or None, status))
        new_id = cur.fetchone()["id"]

        # Insert into shared_goal_members for all non-creator participants
        for mr in member_rows:
            cur.execute("""
                INSERT INTO shared_goal_members (goal_id, user_id, status)
                VALUES (%s, %s, 'pending')
                ON CONFLICT (goal_id, user_id) DO NOTHING
            """, (new_id, mr["id"]))

    conn.commit()

    # Notify all members
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Принять", "callback_data": f"goal:accept:{new_id}"},
        {"text": "❌ Отклонить", "callback_data": f"goal:reject:{new_id}"},
    ]]}
    for mr in member_rows:
        await send_tg_message(
            mr["id"],
            f"🎯 <b>{creator_name}</b> приглашает вас на совместную цель!\n"
            f"<b>{body.emoji} {body.name}</b> · цель: <b>{body.target:,} сум</b>\n"
            f"{'📝 ' + body.note if body.note else ''}\n\n"
            f"Принять предложение? 👇",
            keyboard
        )
    return {"id": new_id, "ok": True, "members": len(member_rows)}

@app.get("/api/shared_goals")
def get_shared_goals(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        # Goals where user is creator OR in shared_goal_members OR legacy partner_id
        cur.execute("""
            SELECT DISTINCT sg.*,
                   u1.first_name AS creator_name, u1.nickname AS creator_nick
            FROM shared_goals sg
            LEFT JOIN users u1 ON sg.creator_id = u1.id
            LEFT JOIN shared_goal_members sgm ON sg.id = sgm.goal_id AND sgm.user_id = %s
            WHERE sg.creator_id=%s OR sg.partner_id=%s OR sgm.user_id=%s
            ORDER BY sg.created_at DESC
        """, (uid, uid, uid, uid))
        rows = cur.fetchall()

        # For each goal, load all members
        result = []
        for r in rows:
            goal_id = r["id"]
            # Get all members from shared_goal_members table
            cur.execute("""
                SELECT u.id, u.first_name, u.nickname, sgm.status AS member_status
                FROM shared_goal_members sgm
                JOIN users u ON sgm.user_id = u.id
                WHERE sgm.goal_id = %s
                ORDER BY sgm.joined_at
            """, (goal_id,))
            members = [{"id": m["id"], "name": m["nickname"] or m["first_name"],
                        "nickname": m["nickname"], "status": m["member_status"]} for m in cur.fetchall()]

            # Determine my member status (for accept/reject)
            my_member_status = None
            for m in members:
                if m["id"] == uid:
                    my_member_status = m["status"]
                    break

            # If no members in new table but has legacy partner_id, build synthetic member list
            if not members and r["partner_id"]:
                cur.execute("SELECT id, first_name, nickname FROM users WHERE id=%s", (r["partner_id"],))
                p = cur.fetchone()
                if p:
                    members = [{"id": p["id"], "name": p["nickname"] or p["first_name"],
                                "nickname": p["nickname"], "status": r["status"] if r["status"] in ("pending","rejected") else "accepted"}]
                    if uid == r["partner_id"]:
                        my_member_status = members[0]["status"]

            result.append({
                "id": r["id"], "name": r["name"], "emoji": r["emoji"],
                "target": r["target"], "saved": r["saved"], "note": r["note"],
                "deadline": str(r["deadline"]) if r["deadline"] else None,
                "status": r["status"],
                "is_mine": r["creator_id"] == uid,
                "my_member_status": my_member_status,
                "creator": {"id": r["creator_id"], "name": r["creator_nick"] or r["creator_name"]},
                "members": members,
            })
    return result

@app.patch("/api/shared_goals/{goal_id}")
async def update_shared_goal(goal_id: int, body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    action = body.get("action")
    me_name = tg_user.get("first_name") or tg_user.get("username") or "Someone"

    with conn.cursor() as cur:
        # Check access: creator, legacy partner, or member
        cur.execute("""
            SELECT sg.* FROM shared_goals sg
            LEFT JOIN shared_goal_members sgm ON sg.id=sgm.goal_id AND sgm.user_id=%s
            WHERE sg.id=%s AND (sg.creator_id=%s OR sg.partner_id=%s OR sgm.user_id=%s)
        """, (uid, goal_id, uid, uid, uid))
        goal = cur.fetchone()
        if not goal:
            raise HTTPException(404, "Goal not found")

        contrib_id = None

        if action in ("accept", "reject"):
            # Check user is a member (not creator) via members table or legacy partner_id
            is_member = (goal["partner_id"] == uid)
            cur.execute("SELECT id FROM shared_goal_members WHERE goal_id=%s AND user_id=%s", (goal_id, uid))
            mem_row = cur.fetchone()
            if mem_row:
                is_member = True

            if not is_member:
                raise HTTPException(403, "Only members can accept/reject")

            new_status = "accepted" if action == "accept" else "rejected"
            if mem_row:
                cur.execute("UPDATE shared_goal_members SET status=%s WHERE goal_id=%s AND user_id=%s",
                            (new_status, goal_id, uid))
                # If all members accepted → activate goal
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted,
                           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected
                    FROM shared_goal_members WHERE goal_id=%s
                """, (goal_id,))
                counts = cur.fetchone()
                if counts["rejected"] > 0:
                    cur.execute("UPDATE shared_goals SET status='rejected' WHERE id=%s", (goal_id,))
                elif counts["accepted"] == counts["total"]:
                    cur.execute("UPDATE shared_goals SET status='active' WHERE id=%s", (goal_id,))
            else:
                # Legacy single-partner path
                goal_new_status = "active" if action == "accept" else "rejected"
                cur.execute("UPDATE shared_goals SET status=%s WHERE id=%s", (goal_new_status, goal_id))

            # Notify creator
            if action == "accept":
                await send_tg_message(goal["creator_id"],
                    f"✅ <b>{me_name}</b> принял вашу совместную цель <b>{goal['emoji']} {goal['name']}</b>!")
            else:
                await send_tg_message(goal["creator_id"],
                    f"❌ <b>{me_name}</b> отклонил приглашение на совместную цель.")

        elif action == "contribute":
            amount = int(body.get("amount", 0))
            label = body.get("label", "Пополнение")
            if amount > 0:
                cur.execute("UPDATE shared_goals SET saved=saved+%s WHERE id=%s", (amount, goal_id))
                cur.execute(
                    "INSERT INTO shared_goal_contributions (goal_id, user_id, amount, label) VALUES (%s, %s, %s, %s) RETURNING id",
                    (goal_id, uid, amount, label)
                )
                contrib_id = cur.fetchone()["id"]
                cur.execute("SELECT saved, target FROM shared_goals WHERE id=%s", (goal_id,))
                updated = cur.fetchone()
                if updated["saved"] >= updated["target"]:
                    cur.execute("UPDATE shared_goals SET status='completed' WHERE id=%s", (goal_id,))

        elif action == "complete":
            cur.execute("UPDATE shared_goals SET status='completed' WHERE id=%s", (goal_id,))

        else:
            raise HTTPException(400, "Invalid action")

    conn.commit()
    return {"ok": True, "contribution_id": contrib_id}

@app.get("/api/shared_goals/{goal_id}/contributions")
def get_shared_goal_contributions(goal_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        # Verify user is part of this goal (creator, legacy partner, or member)
        cur.execute("""
            SELECT sg.id FROM shared_goals sg
            LEFT JOIN shared_goal_members sgm ON sg.id=sgm.goal_id AND sgm.user_id=%s
            WHERE sg.id=%s AND (sg.creator_id=%s OR sg.partner_id=%s OR sgm.user_id=%s)
        """, (uid, goal_id, uid, uid, uid))
        if not cur.fetchone():
            raise HTTPException(403, "Access denied")
        cur.execute("""
            SELECT sgc.id, sgc.amount, sgc.label, sgc.created_at,
                   u.first_name, u.nickname, u.id AS contrib_user_id
            FROM shared_goal_contributions sgc
            JOIN users u ON sgc.user_id = u.id
            WHERE sgc.goal_id = %s
            ORDER BY sgc.created_at DESC
        """, (goal_id,))
        rows = cur.fetchall()
    return [{
        "id": r["id"],
        "amount": r["amount"],
        "label": r["label"] or "Пополнение",
        "date": r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "",
        "user_name": r["nickname"] or r["first_name"] or "User",
        "is_me": r["contrib_user_id"] == uid,
    } for r in rows]

# ── Alerts summary (for home-screen widget + nav badges) ─────────────────────
@app.get("/api/alerts")
def get_alerts(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM friends WHERE addressee_id=%s AND status='pending'", (uid,))
        friends_pending = int(cur.fetchone()["cnt"])
        cur.execute("SELECT COUNT(*) as cnt FROM social_debts WHERE counterpart_id=%s AND status='pending'", (uid,))
        debts_pending = int(cur.fetchone()["cnt"])
        cur.execute("""
            SELECT COUNT(DISTINCT sg.id) as cnt
            FROM shared_goals sg
            LEFT JOIN shared_goal_members sgm ON sg.id=sgm.goal_id AND sgm.user_id=%s
            WHERE (sg.partner_id=%s OR sgm.user_id=%s)
              AND sg.status='pending' AND sg.creator_id!=%s
              AND (sgm.status='pending' OR (sgm.id IS NULL AND sg.partner_id=%s))
        """, (uid, uid, uid, uid, uid))
        goals_pending = int(cur.fetchone()["cnt"])
    return {
        "friends": friends_pending,
        "debts":   debts_pending,
        "goals":   goals_pending,
        "total":   friends_pending + debts_pending + goals_pending,
    }

# ── Personal Goals ────────────────────────────────────────────────────────────
def _goal_row(r) -> dict:
    return {
        "id": r["id"],
        "emoji": r["emoji"],
        "name": r["name"],
        "target": r["target"],
        "saved": r["saved"],
        "duration": r["duration"],
        "startY": r["start_year"],
        "startM": r["start_month"],
        "deadlineY": r["deadline_year"],
        "deadlineM": r["deadline_month"],
        "onPlan": True,
        "done": r["done"],
        "doneDate": str(r["done_date"]) if r["done_date"] else None,
        "contributions": r["contributions"] or [],
    }

@app.get("/api/personal_goals")
def get_personal_goals(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM personal_goals WHERE user_id=%s ORDER BY created_at DESC", (uid,))
        rows = cur.fetchall()
    return [_goal_row(r) for r in rows]

@app.post("/api/personal_goals")
def create_personal_goal(body: PersonalGoalCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO personal_goals
              (user_id, emoji, name, target, duration, start_year, start_month, deadline_year, deadline_month)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.emoji, body.name, body.target, body.duration,
              body.start_year, body.start_month, body.deadline_year, body.deadline_month))
        row = cur.fetchone()
    conn.commit()
    return _goal_row(row)

@app.patch("/api/personal_goals/{goal_id}")
async def patch_personal_goal(goal_id: int, body: PersonalGoalPatch, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    # Fetch old values for milestone check
    with conn.cursor() as cur:
        cur.execute("SELECT saved, target, name, emoji FROM personal_goals WHERE id=%s AND user_id=%s", (goal_id, uid))
        old = cur.fetchone()
        if not old:
            raise HTTPException(404, "Goal not found")

    fields, vals = [], []
    if body.saved is not None:
        fields.append("saved=%s"); vals.append(body.saved)
    if body.done is not None:
        fields.append("done=%s"); vals.append(body.done)
    if body.done_date is not None:
        fields.append("done_date=%s"); vals.append(body.done_date)
    if body.contributions is not None:
        fields.append("contributions=%s"); vals.append(json.dumps(body.contributions))
    if body.name is not None:
        fields.append("name=%s"); vals.append(body.name)
    if body.emoji is not None:
        fields.append("emoji=%s"); vals.append(body.emoji)
    if body.target is not None:
        fields.append("target=%s"); vals.append(body.target)
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields.append("updated_at=NOW()")
    vals += [goal_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE personal_goals SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
    conn.commit()

    # Fire milestone notification if saved changed
    if body.saved is not None:
        asyncio.create_task(_check_goal_milestone(
            uid, old["name"], old["emoji"],
            int(old["saved"]), int(body.saved), int(body.target or old["target"])
        ))

    # Award XP + achievement when goal is completed for first time
    if body.done and not old["done"]:
        XP_GOAL_DONE = 100
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET xp=xp+%s WHERE id=%s", (XP_GOAL_DONE, uid))
            add_xp_log(cur, uid, "🌟", "Цель выполнена!", old["name"], XP_GOAL_DONE)
        conn.commit()
        new_achs = check_achievements(conn, uid)
        conn.commit()
        if new_achs:
            import threading as _t2
            _t2.Thread(target=notify_achievements, args=(uid, new_achs), daemon=True).start()
        # Notify user
        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=profile"}}]]}
        asyncio.create_task(send_tg_message(uid,
            f"🌟 <b>Цель выполнена!</b>\n\n"
            f"{old['emoji']} {old['name']}\n"
            f"Вы достигли своей цели! +{XP_GOAL_DONE} XP 🎉", kb))

    return _goal_row(row)

@app.delete("/api/personal_goals/{goal_id}")
def delete_personal_goal(goal_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM personal_goals WHERE id=%s AND user_id=%s RETURNING id", (goal_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Goal not found")
    conn.commit()
    return {"deleted": goal_id}

# ── Personal Debts ────────────────────────────────────────────────────────────
def _debt_row(r) -> dict:
    amount = r["amount"] or 0
    paid_amount = r.get("paid_amount") or 0
    return {
        "id": r["id"],
        "name": r["name"],
        "amount": amount,
        "paidAmount": paid_amount,
        "remaining": max(0, amount - paid_amount),
        "type": r["type"],
        "note": r["note"],
        "dueDate": str(r["due_date"]) if r["due_date"] else None,
        "paid": r["paid"],
    }

@app.get("/api/personal_debts")
def get_personal_debts(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM personal_debts WHERE user_id=%s ORDER BY created_at DESC", (uid,))
        rows = cur.fetchall()
    return [_debt_row(r) for r in rows]

@app.post("/api/personal_debts")
def create_personal_debt(body: PersonalDebtCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO personal_debts (user_id, name, amount, type, note, due_date)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.name, body.amount, body.type, body.note, body.due_date or None))
        row = cur.fetchone()
    conn.commit()
    return _debt_row(row)

@app.patch("/api/personal_debts/{debt_id}")
async def patch_personal_debt(debt_id: int, body: PersonalDebtPatch, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT name, amount, type, paid FROM personal_debts WHERE id=%s AND user_id=%s", (debt_id, uid))
        old = cur.fetchone()
        if not old:
            raise HTTPException(404, "Debt not found")

    fields, vals = [], []
    if body.paid is not None:
        fields.append("paid=%s"); vals.append(body.paid)
    if body.name is not None:
        fields.append("name=%s"); vals.append(body.name)
    if body.amount is not None:
        fields.append("amount=%s"); vals.append(body.amount)
    if body.note is not None:
        fields.append("note=%s"); vals.append(body.note)
    if body.due_date is not None:
        fields.append("due_date=%s"); vals.append(body.due_date)
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields.append("updated_at=NOW()")
    vals += [debt_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE personal_debts SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Debt not found")
    conn.commit()

    # Celebrate + award XP when debt is fully paid
    if body.paid and not old["paid"]:
        XP_DEBT_PAID = 75
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET xp=xp+%s WHERE id=%s", (XP_DEBT_PAID, uid))
            add_xp_log(cur, uid, "🤝", "Долг погашен!", old["name"], XP_DEBT_PAID)
        conn.commit()
        new_achs = check_achievements(conn, uid)
        conn.commit()
        if new_achs:
            import threading as _t3
            _t3.Thread(target=notify_achievements, args=(uid, new_achs), daemon=True).start()
        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=profile"}}]]}
        amt = f"{int(old['amount']):,}".replace(",", " ")
        verb = "отдан" if old["type"] == "lent" else "погашен"
        asyncio.create_task(send_tg_message(uid,
            f"✅ <b>Долг {verb}!</b>\n\n"
            f"💸 {old['name']} — {amt} сум\n"
            f"Отличная работа, долгов меньше! +{XP_DEBT_PAID} XP 🎉", kb))

    return _debt_row(row)

@app.post("/api/personal_debts/{debt_id}/pay")
def pay_personal_debt(debt_id: int, body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Pay a debt in part or full.
    body: { amount, affects_balance (bool), account }
    - affects_balance=True  → money leaves your pocket: creates an EXPENSE transaction (balance ↓) + reduces debt.
    - affects_balance=False → debt closed from external money: NO transaction, balance untouched, debt ↓.
    """
    uid = tg_user["id"]
    pay = int(body.get("amount") or 0)
    affects_balance = bool(body.get("affects_balance", True))
    account = (body.get("account") or "Наличка").strip()
    if pay <= 0:
        raise HTTPException(400, "Сумма должна быть больше нуля")
    with conn.cursor() as cur:
        cur.execute("SELECT name, amount, paid_amount, type, paid FROM personal_debts WHERE id=%s AND user_id=%s", (debt_id, uid))
        d = cur.fetchone()
        if not d:
            raise HTTPException(404, "Debt not found")
        amount = d["amount"] or 0
        already = d["paid_amount"] or 0
        remaining = max(0, amount - already)
        pay = min(pay, remaining)  # never overpay
        if pay <= 0:
            raise HTTPException(400, "Долг уже погашен")
        new_paid = already + pay
        fully = new_paid >= amount
        cur.execute("UPDATE personal_debts SET paid_amount=%s, paid=%s, updated_at=NOW() WHERE id=%s AND user_id=%s",
                    (new_paid, fully, debt_id, uid))
        # Optionally create a balance-affecting expense transaction
        if affects_balance:
            today_str = date.today().isoformat()
            now_str = datetime.now().strftime("%H:%M")
            cur.execute("""
                INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,account,source)
                VALUES (%s,'expense','🤝',%s,%s,%s,%s,%s,'UZS',%s,'debt')
            """, (uid, "Долг: " + d["name"], "Погашение долга", pay, today_str, now_str, account))
    conn.commit()
    # Celebrate full repayment
    if fully and not d["paid"]:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET xp=xp+75 WHERE id=%s", (uid,))
            add_xp_log(cur, uid, "🤝", "Долг погашен!", d["name"], 75)
        conn.commit()
        check_achievements(conn, uid)
        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}}]]}
        send_tg_message_sync(uid,
            f"✅ <b>Долг погашен!</b>\n\n🤝 {d['name']}\nОтличная работа — долгов меньше! +75 XP 🎉", kb)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM personal_debts WHERE id=%s", (debt_id,))
        row = cur.fetchone()
    return _debt_row(row)

@app.delete("/api/personal_debts/{debt_id}")
def delete_personal_debt(debt_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM personal_debts WHERE id=%s AND user_id=%s RETURNING id", (debt_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Debt not found")
    conn.commit()
    return {"deleted": debt_id}

# ── Savings Jars (Копилки) ────────────────────────────────────────────────────
def _jar_row(r) -> dict:
    return {
        "id":      r["id"],
        "name":    r["name"],
        "emoji":   r["emoji"],
        "balance": r["balance"],
        "target":  r["target"],
        "note":    r["note"],
    }

@app.get("/api/savings")
def get_savings_jars(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM savings_jars WHERE user_id=%s ORDER BY created_at DESC", (uid,))
        rows = cur.fetchall()
    return [_jar_row(r) for r in rows]

@app.post("/api/savings")
def create_savings_jar(body: SavingsJarCreate, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    upsert_user(conn, tg_user)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO savings_jars (user_id, name, emoji, balance, target, note)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, body.name, body.emoji, body.balance, body.target, body.note))
        row = cur.fetchone()
    conn.commit()
    return _jar_row(row)

@app.patch("/api/savings/{jar_id}")
def patch_savings_jar(jar_id: int, body: SavingsJarPatch, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    if body.name    is not None: fields.append("name=%s");    vals.append(body.name)
    if body.emoji   is not None: fields.append("emoji=%s");   vals.append(body.emoji)
    if body.balance is not None: fields.append("balance=%s"); vals.append(body.balance)
    if body.target  is not None: fields.append("target=%s");  vals.append(body.target)
    if body.note    is not None: fields.append("note=%s");    vals.append(body.note)
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields.append("updated_at=NOW()")
    vals += [jar_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE savings_jars SET {','.join(fields)} WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Jar not found")
    conn.commit()
    return _jar_row(row)

@app.delete("/api/savings/{jar_id}")
def delete_savings_jar(jar_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM savings_jars WHERE id=%s AND user_id=%s RETURNING id", (jar_id, uid))
        if not cur.fetchone():
            raise HTTPException(404, "Jar not found")
    conn.commit()
    return {"deleted": jar_id}

# ── Admin: remote-control task queue ─────────────────────────────────────────
def _admin_auth(token: str):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

@app.post("/api/admin/task")
def admin_create_task(body: dict, token: str = "", conn=Depends(db)):
    """Bot calls this to enqueue a /claude task."""
    _admin_auth(token)
    task = (body.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "task required")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_tasks(task) VALUES (%s) RETURNING id, task, status, created_at",
                (task,)
            )
            row = cur.fetchone()
    return dict(row)

@app.get("/api/admin/task/next")
def admin_next_task(token: str = "", conn=Depends(db)):
    """Cron job calls this to claim the next pending task."""
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE claude_tasks SET status='running', updated_at=NOW()
                WHERE id = (
                    SELECT id FROM claude_tasks WHERE status='pending'
                    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                RETURNING id, task, created_at
            """)
            row = cur.fetchone()
    if not row:
        return {"task": None}
    return dict(row)

@app.post("/api/admin/task/{task_id}/done")
def admin_task_done(task_id: int, body: dict, token: str = "", conn=Depends(db)):
    """Cron job calls this after completing a task."""
    _admin_auth(token)
    status = body.get("status", "done")   # "done" or "failed"
    result = body.get("result", "")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE claude_tasks SET status=%s, result=%s, updated_at=NOW() WHERE id=%s",
                (status, result, task_id)
            )
    return {"ok": True}

@app.get("/api/admin/users")
def admin_list_users(token: str = "", conn=Depends(db)):
    """List all users with all dashboard-relevant fields."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, first_name, last_name, username, nickname, xp, level,
                   streak, streak_record, last_log_date, created_at,
                   language, rank_visible, notifs, weekly_report, notif_overtake,
                   budget_plan_active, budget_plan_days, budget_plan_daily, budget_plan_activated_at,
                   sms_token IS NOT NULL AS has_shortcut_token,
                   ref_code, referred_by,
                   (SELECT COUNT(*) FROM transactions t WHERE t.user_id=users.id) AS tx_count,
                   (SELECT COUNT(*) FROM transactions t WHERE t.user_id=users.id AND t.date_str=CURRENT_DATE) AS logged_today
            FROM users
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, token: str = "", conn=Depends(db)):
    """Hard-delete a user and all their data — for dev/testing only."""
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s RETURNING id, first_name, username", (user_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return {"deleted": dict(row)}

# ── Admin: transactions ───────────────────────────────────────────────────────

@app.get("/api/admin/transactions")
def admin_list_transactions(token: str = "", user_id: int = None, conn=Depends(db)):
    """All transactions, optionally filtered by user_id."""
    _admin_auth(token)
    with conn.cursor() as cur:
        if user_id:
            cur.execute("""
                SELECT t.id, t.user_id, u.first_name AS user_name, t.type,
                       t.amount, t.cat_icon AS cat, t.cat_name, t.note,
                       t.account, t.date_str, t.time_str, t.source
                FROM transactions t JOIN users u ON t.user_id=u.id
                WHERE t.user_id=%s ORDER BY t.date_str DESC, t.time_str DESC LIMIT 500
            """, (user_id,))
        else:
            cur.execute("""
                SELECT t.id, t.user_id, u.first_name AS user_name, t.type,
                       t.amount, t.cat_icon AS cat, t.cat_name, t.note,
                       t.account, t.date_str, t.time_str, t.source
                FROM transactions t JOIN users u ON t.user_id=u.id
                ORDER BY t.date_str DESC, t.time_str DESC LIMIT 1000
            """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/personal_goals")
def admin_list_personal_goals(token: str = "", conn=Depends(db)):
    """All personal goals across all users."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pg.*, COALESCE(u.first_name, u.username, u.id::text) AS user_name
            FROM personal_goals pg
            JOIN users u ON u.id = pg.user_id
            ORDER BY pg.created_at DESC
        """)
        rows = cur.fetchall()
    result = []
    for r in rows:
        d = _goal_row(r)
        d["user_name"] = r["user_name"]
        result.append(d)
    return result

@app.get("/api/admin/goals")
def admin_list_goals(token: str = "", conn=Depends(db)):
    """Shared goals only."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sg.id, sg.creator_id, uc.first_name AS creator_name,
                   sg.partner_id, up.first_name AS partner_name,
                   sg.name, sg.emoji, sg.target, sg.saved, sg.status,
                   sg.deadline, sg.created_at
            FROM shared_goals sg
            JOIN users uc ON sg.creator_id=uc.id
            LEFT JOIN users up ON sg.partner_id=up.id
            ORDER BY sg.created_at DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/debts")
def admin_list_debts(token: str = "", conn=Depends(db)):
    """All debt records across all users."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id, d.creator_id, uc.first_name AS creator_name,
                   d.counterpart_id, up.first_name AS counterpart_name,
                   d.debt_type, d.amount, d.note, d.status, d.due_date, d.created_at
            FROM social_debts d
            LEFT JOIN users uc ON d.creator_id=uc.id
            LEFT JOIN users up ON d.counterpart_id=up.id
            ORDER BY d.created_at DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.delete("/api/admin/transactions/{tx_id}")
def admin_delete_transaction(tx_id: int, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE id=%s RETURNING id", (tx_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Transaction not found")
    return {"deleted": tx_id}

@app.put("/api/admin/transactions/{tx_id}")
def admin_edit_transaction(tx_id: int, body: dict, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    fields = []
    vals = []
    for col in ("type","cat_name","cat_icon","amount","note","account","date_str","time_str"):
        if col in body:
            fields.append(f"{col}=%s")
            vals.append(body[col])
    if not fields:
        raise HTTPException(400, "Nothing to update")
    vals.append(tx_id)
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE transactions SET {','.join(fields)} WHERE id=%s RETURNING id", vals)
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Transaction not found")
    return {"updated": tx_id}

@app.delete("/api/admin/debts/{debt_id}")
def admin_delete_debt(debt_id: int, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_debts WHERE id=%s RETURNING id", (debt_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Debt not found")
    return {"deleted": debt_id}

@app.put("/api/admin/debts/{debt_id}")
def admin_edit_debt(debt_id: int, body: dict, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    fields = []
    vals = []
    for col in ("amount","note","status","debt_type","due_date"):
        if col in body:
            fields.append(f"{col}=%s")
            vals.append(body[col])
    if not fields:
        raise HTTPException(400, "Nothing to update")
    vals.append(debt_id)
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE social_debts SET {','.join(fields)} WHERE id=%s RETURNING id", vals)
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Debt not found")
    return {"updated": debt_id}

@app.get("/api/admin/budgets")
def admin_list_budgets(token: str = "", conn=Depends(db)):
    """All budgets across all users with current-month spending."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.id, b.user_id, COALESCE(u.first_name, u.username, u.id::text) AS user_name,
                   b.name, b.icon, b.limit_amt, b.color, b.created_at,
                   COALESCE(
                     (SELECT SUM(t.amount) FROM transactions t
                      WHERE t.user_id=b.user_id AND t.type='expense' AND t.cat_name=b.name
                        AND t.date_str >= date_trunc('month', CURRENT_DATE)::date),
                   0) AS spent
            FROM budgets b
            JOIN users u ON u.id = b.user_id
            ORDER BY b.created_at DESC
        """)
        rows = cur.fetchall()
    return [{
        "id":        r["id"],
        "user_id":   r["user_id"],
        "user_name": r["user_name"],
        "name":      r["name"],
        "icon":      r["icon"],
        "limit":     r["limit_amt"],
        "spent":     int(r["spent"]),
        "pct":       round(int(r["spent"]) / r["limit_amt"] * 100) if r["limit_amt"] else 0,
        "color":     r["color"],
        "created_at": str(r["created_at"])[:10] if r["created_at"] else None,
    } for r in rows]

@app.delete("/api/admin/goals/{goal_id}")
def admin_delete_shared_goal(goal_id: int, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM shared_goals WHERE id=%s RETURNING id", (goal_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Goal not found")
    return {"deleted": goal_id}

@app.delete("/api/admin/personal_goals/{goal_id}")
def admin_delete_personal_goal(goal_id: int, token: str = "", conn=Depends(db)):
    """Delete a personal goal by ID."""
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM personal_goals WHERE id=%s RETURNING id", (goal_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Personal goal not found")
    return {"deleted": goal_id}

@app.get("/api/admin/personal_debts")
def admin_list_personal_debts(token: str = "", conn=Depends(db)):
    """All personal debts across all users."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pd.*, COALESCE(u.first_name, u.username, u.id::text) AS user_name
            FROM personal_debts pd
            JOIN users u ON u.id = pd.user_id
            ORDER BY pd.created_at DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.get("/api/admin/savings")
def admin_list_savings(token: str = "", conn=Depends(db)):
    """All savings jars across all users."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT sj.*, COALESCE(u.first_name, u.username, u.id::text) AS user_name
            FROM savings_jars sj
            JOIN users u ON u.id = sj.user_id
            ORDER BY sj.created_at DESC
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.delete("/api/admin/personal_debts/{debt_id}")
def admin_delete_personal_debt(debt_id: int, token: str = "", conn=Depends(db)):
    """Delete a personal debt by ID."""
    _admin_auth(token)
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM personal_debts WHERE id=%s RETURNING id", (debt_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Personal debt not found")
    return {"deleted": debt_id}

@app.get("/api/admin/ranks")
def admin_ranks(token: str = "", conn=Depends(db)):
    """Leaderboard with rank visibility info for admin management."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, first_name, username, nickname, xp, level, streak,
                   rank_visible, created_at
            FROM users
            ORDER BY xp DESC
            LIMIT 100
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.patch("/api/admin/ranks/{user_id}")
def admin_update_rank_visibility(user_id: int, body: dict, token: str = "", conn=Depends(db)):
    """Override a user's rank visibility from admin."""
    _admin_auth(token)
    rv = body.get("rank_visible")
    if rv not in ("everyone", "friends", "nobody", "anonymous"):
        raise HTTPException(400, "rank_visible must be everyone/friends/nobody/anonymous")
    with conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET rank_visible=%s WHERE id=%s RETURNING id", (rv, user_id))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return {"updated": user_id, "rank_visible": rv}

@app.get("/api/admin/log_sources")
def admin_log_sources(token: str = "", conn=Depends(db)):
    """Breakdown of how users log transactions: app vs shortcut vs voice vs sms."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(source,'app') AS source, COUNT(*) AS cnt
            FROM transactions GROUP BY source ORDER BY cnt DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS total FROM transactions")
        total = cur.fetchone()["total"] or 1
        # Active users today
        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE date_str=CURRENT_DATE")
        active_today = cur.fetchone()["cnt"]
        # New users this week
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= NOW() - INTERVAL '7 days'")
        new_week = cur.fetchone()["cnt"]
        # Transactions today
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE date_str=CURRENT_DATE")
        txs_today = cur.fetchone()["cnt"]
    for r in rows:
        r["pct"] = round(r["cnt"] / total * 100)
    return {"sources": rows, "total": total,
            "active_today": active_today, "new_week": new_week, "txs_today": txs_today}

@app.get("/api/admin/finlyfast")
def admin_finlyfast(token: str = "", conn=Depends(db)):
    """FinlyFast shortcut usage stats."""
    _admin_auth(token)
    with conn.cursor() as cur:
        # Aggregate stats
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE sms_token IS NOT NULL) AS token_count,
                COUNT(*) FILTER (WHERE sms_token IS NOT NULL
                    AND EXISTS (SELECT 1 FROM transactions t WHERE t.user_id=u.id AND t.source='shortcut')
                ) AS active_users,
                (SELECT COUNT(*) FROM transactions WHERE source='shortcut') AS shortcut_txs,
                (SELECT COUNT(*) FROM transactions WHERE source='voice') AS voice_txs
            FROM users u
        """)
        stats = dict(cur.fetchone())
        # Per-user table
        cur.execute("""
            SELECT u.id, u.first_name, u.username, u.nickname,
                   LEFT(u.sms_token,8) AS token_prefix,
                   COUNT(t.id) FILTER (WHERE t.source='shortcut') AS shortcut_count,
                   COUNT(t.id) FILTER (WHERE t.source='voice')    AS voice_count,
                   MAX(t.created_at) FILTER (WHERE t.source IN ('shortcut','voice')) AS last_used,
                   u.xp, u.level, u.streak
            FROM users u
            LEFT JOIN transactions t ON t.user_id = u.id
            WHERE u.sms_token IS NOT NULL
            GROUP BY u.id
            ORDER BY shortcut_count DESC, last_used DESC NULLS LAST
        """)
        users = [dict(r) for r in cur.fetchall()]
    for u in users:
        if u.get("last_used"):
            u["last_used"] = u["last_used"].isoformat()
    return {"stats": stats, "users": users}

@app.get("/api/admin/xp_logs")
def admin_xp_logs(token: str = "", user_id: int = None, limit: int = 100, conn=Depends(db)):
    """XP log entries, optionally filtered by user_id."""
    _admin_auth(token)
    with conn.cursor() as cur:
        if user_id:
            cur.execute("""
                SELECT xl.id, xl.user_id, u.first_name AS user_name,
                       xl.icon, xl.label, xl.sub, xl.pts, xl.date_str, xl.created_at
                FROM xp_log xl JOIN users u ON xl.user_id=u.id
                WHERE xl.user_id=%s ORDER BY xl.created_at DESC LIMIT %s
            """, (user_id, limit))
        else:
            cur.execute("""
                SELECT xl.id, xl.user_id, u.first_name AS user_name,
                       xl.icon, xl.label, xl.sub, xl.pts, xl.date_str, xl.created_at
                FROM xp_log xl JOIN users u ON xl.user_id=u.id
                ORDER BY xl.created_at DESC LIMIT %s
            """, (limit,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]

@app.patch("/api/admin/xp_logs/{log_id}")
def admin_edit_xp_log(log_id: int, body: dict, token: str = "", conn=Depends(db)):
    """Edit XP log entry pts — recalculates user total XP + level."""
    _admin_auth(token)
    pts = body.get("pts")
    if pts is None:
        raise HTTPException(400, "pts field required")
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, pts FROM xp_log WHERE id=%s", (log_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "XP log not found")
        user_id = row["user_id"]
        old_pts = row["pts"]
        diff = pts - old_pts
        cur.execute("UPDATE xp_log SET pts=%s WHERE id=%s", (pts, log_id))
        # Apply diff to preserve any XP not tracked in xp_log (referrals, etc.)
        cur.execute("UPDATE users SET xp=GREATEST(0, xp+%s) WHERE id=%s RETURNING xp", (diff, user_id))
        new_xp = int(cur.fetchone()["xp"])
        new_level = max(1, new_xp // 500 + 1)
        cur.execute("UPDATE users SET level=%s WHERE id=%s", (new_level, user_id))
    conn.commit()
    return {"updated": log_id, "new_xp": new_xp, "new_level": new_level}

@app.post("/api/admin/db_cleanup")
def admin_db_cleanup(token: str = "", conn=Depends(db)):
    """Delete orphaned rows and vacuum the database."""
    _admin_auth(token)
    results = {}
    with conn.cursor() as cur:
        # Orphaned xp_log rows
        cur.execute("DELETE FROM xp_log WHERE user_id NOT IN (SELECT id FROM users)")
        results["xp_log_deleted"] = cur.rowcount
        # Orphaned transactions
        cur.execute("DELETE FROM transactions WHERE user_id NOT IN (SELECT id FROM users)")
        results["transactions_deleted"] = cur.rowcount
        # Orphaned feedback
        cur.execute("DELETE FROM feedback WHERE user_id NOT IN (SELECT id FROM users)")
        results["feedback_deleted"] = cur.rowcount
        # Orphaned budgets
        cur.execute("DELETE FROM budgets WHERE user_id NOT IN (SELECT id FROM users)")
        results["budgets_deleted"] = cur.rowcount
        # Orphaned goals
        cur.execute("DELETE FROM goals WHERE user_id NOT IN (SELECT id FROM users)")
        results["goals_deleted"] = cur.rowcount
        # Orphaned debts
        cur.execute("DELETE FROM debts WHERE user_id NOT IN (SELECT id FROM users)")
        results["debts_deleted"] = cur.rowcount
        # Orphaned categories
        cur.execute("DELETE FROM categories WHERE user_id NOT IN (SELECT id FROM users)")
        results["categories_deleted"] = cur.rowcount
    conn.commit()
    # VACUUM cannot run inside a transaction — use autocommit
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE")
        conn.autocommit = False
        results["vacuum"] = "ok"
    except Exception as e:
        results["vacuum"] = str(e)
    return results

@app.get("/api/admin/feature_usage")
def admin_feature_usage(token: str = "", conn=Depends(db)):
    """Per-feature adoption: how many users use each feature."""
    _admin_auth(token)
    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    with conn.cursor() as cur:
        def q(sql, *args):
            cur.execute(sql, args or ())
            return cur.fetchone()["cnt"]
        total = q("SELECT COUNT(*) AS cnt FROM users")
        return {
            "total_users": total,
            "active_7d":  q("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE date_str >= %s", week_ago),
            "active_30d": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE date_str >= %s", month_ago),
            "features": [
                {"name":"Goals",         "icon":"🎯", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM personal_goals")},
                {"name":"Debts",         "icon":"🤝", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM personal_debts")},
                {"name":"Savings jars",  "icon":"🏺", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM savings_jars")},
                {"name":"Budgets",       "icon":"📊", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM budgets")},
                {"name":"Budget plan",   "icon":"📋", "users": q("SELECT COUNT(*) AS cnt FROM users WHERE budget_plan_active=TRUE")},
                {"name":"Friends",       "icon":"🤗", "users": q("SELECT COUNT(DISTINCT requester_id) AS cnt FROM friends WHERE status='accepted'")},
                {"name":"FinlyFast",     "icon":"⚡", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE source='shortcut'")},
                {"name":"Voice log",     "icon":"🎤", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE source='voice'")},
                {"name":"Feedback",      "icon":"💬", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM feedback")},
                {"name":"Custom cats",   "icon":"🗂", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM categories")},
                {"name":"Recurring tx",  "icon":"🔁", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE recurring=TRUE")},
                {"name":"Nickname set",  "icon":"🏷", "users": q("SELECT COUNT(*) AS cnt FROM users WHERE nickname IS NOT NULL AND nickname != ''")},
                {"name":"Achievements",  "icon":"🏆", "users": q("SELECT COUNT(DISTINCT user_id) AS cnt FROM achievements")},
            ]
        }

@app.get("/api/admin/db_stats")
def admin_db_stats(token: str = "", conn=Depends(db)):
    """Row counts and sizes for all app tables."""
    _admin_auth(token)
    tables = [
        'users','transactions','drafts','budgets','categories',
        'social_debts','friends','shared_goals','shared_goal_contributions',
        'personal_goals','personal_debts','savings_jars','xp_log','claude_tasks'
    ]
    result = []
    with conn.cursor() as cur:
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {t}")
                cnt = cur.fetchone()["cnt"]
                cur.execute(
                    "SELECT pg_size_pretty(pg_total_relation_size(%s)) AS sz", (t,)
                )
                sz = cur.fetchone()["sz"]
                result.append({"table": t, "rows": cnt, "size": sz})
            except Exception:
                result.append({"table": t, "rows": 0, "size": "—"})
    return result

@app.get("/api/admin/tasks")
def admin_list_tasks(token: str = "", limit: int = 20, conn=Depends(db)):
    """List recent tasks — useful for the admin to review history."""
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, task, status, result, created_at, updated_at "
            "FROM claude_tasks ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]

# ── Admin: analytics dashboard ────────────────────────────────────────────────
@app.get("/api/admin/analytics")
def admin_analytics(token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn.cursor() as cur:
        # Daily registrations — last 30 days
        cur.execute("""
            SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS cnt
            FROM users WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY day ORDER BY day
        """)
        registrations = [{"day": str(r["day"]), "cnt": r["cnt"]} for r in cur.fetchall()]

        # Daily transactions — last 30 days
        cur.execute("""
            SELECT date_str AS day, COUNT(*) AS cnt,
                   COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
            FROM transactions
            WHERE date_str >= CURRENT_DATE - 30
            GROUP BY date_str ORDER BY date_str
        """)
        tx_by_day = [{"day": str(r["day"]), "cnt": r["cnt"],
                      "income": int(r["income"]), "expense": int(r["expense"])} for r in cur.fetchall()]

        # Top 10 expense categories all-time
        cur.execute("""
            SELECT cat_name, COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total
            FROM transactions WHERE type='expense'
            GROUP BY cat_name ORDER BY total DESC LIMIT 10
        """)
        top_cats = [{"name": r["cat_name"], "cnt": r["cnt"], "total": int(r["total"])} for r in cur.fetchall()]

        # Active users 7d / 30d
        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE date_str >= CURRENT_DATE - 7")
        active_7d = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE date_str >= CURRENT_DATE - 30")
        active_30d = cur.fetchone()["cnt"]

        # User aggregate stats
        cur.execute("SELECT COUNT(*) AS total, AVG(xp) AS avg_xp, MAX(xp) AS max_xp, AVG(level) AS avg_level FROM users")
        ua = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE nickname IS NOT NULL AND nickname != ''")
        with_nick = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE streak > 0")
        with_streak = cur.fetchone()["cnt"]

        # New users today / this week
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= CURRENT_DATE")
        new_today = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= date_trunc('week', CURRENT_DATE)")
        new_week = cur.fetchone()["cnt"]

        # Total income / expense all-time
        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0)  AS total_inc,
                   COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS total_exp
            FROM transactions
        """)
        totals = cur.fetchone()

        # Level distribution
        cur.execute("SELECT level, COUNT(*) AS cnt FROM users GROUP BY level ORDER BY level")
        level_dist = [{"level": r["level"], "cnt": r["cnt"]} for r in cur.fetchall()]

        # Tx count buckets
        cur.execute("""
            SELECT CASE
              WHEN cnt=0 THEN '0 txs' WHEN cnt<=5 THEN '1–5' WHEN cnt<=20 THEN '6–20'
              WHEN cnt<=100 THEN '21–100' ELSE '100+' END AS bucket, COUNT(*) AS users
            FROM (SELECT user_id, COUNT(*) AS cnt FROM transactions GROUP BY user_id) x
            GROUP BY bucket ORDER BY MIN(cnt)
        """)
        tx_buckets = [{"bucket": r["bucket"], "users": r["users"]} for r in cur.fetchall()]

    return {
        "registrations": registrations,
        "tx_by_day":     tx_by_day,
        "top_cats":      top_cats,
        "active_7d":     active_7d,
        "active_30d":    active_30d,
        "total_users":   ua["total"],
        "avg_xp":        round(ua["avg_xp"] or 0),
        "max_xp":        ua["max_xp"] or 0,
        "avg_level":     round(float(ua["avg_level"] or 1), 1),
        "with_nickname": with_nick,
        "with_streak":   with_streak,
        "new_today":     new_today,
        "new_week":      new_week,
        "total_income":  int(totals["total_inc"]),
        "total_expense": int(totals["total_exp"]),
        "level_dist":    level_dist,
        "tx_buckets":    tx_buckets,
    }

# ── Admin: cards & SMS stats ──────────────────────────────────────────────────
@app.get("/api/admin/cards_stats")
def admin_cards_stats(token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM bank_cards")
        total_cards = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM bank_cards")
        users_with_cards = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE sms_token IS NOT NULL")
        users_with_token = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM users")
        total_users = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM sms_pending")
        pending_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM bank_cards WHERE auto_log=TRUE")
        auto_log_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM bank_cards WHERE include_in_balance=TRUE")
        in_balance_count = cur.fetchone()["cnt"]

        # Cards by bank
        cur.execute("SELECT bank, COUNT(*) AS cnt FROM bank_cards GROUP BY bank ORDER BY cnt DESC")
        by_bank = [{"bank": r["bank"], "cnt": r["cnt"]} for r in cur.fetchall()]

        # Card registrations last 30 days
        cur.execute("""
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM bank_cards WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY day ORDER BY day
        """)
        card_reg = [{"day": str(r["day"]), "cnt": r["cnt"]} for r in cur.fetchall()]

        # Users who set up at least one card — with last SMS activity
        cur.execute("""
            SELECT u.id, u.first_name, u.username,
                   COUNT(bc.id) AS card_count,
                   MAX(bc.updated_at) AS last_update,
                   (u.sms_token IS NOT NULL) AS has_token
            FROM users u
            JOIN bank_cards bc ON bc.user_id = u.id
            GROUP BY u.id, u.first_name, u.username, u.sms_token
            ORDER BY last_update DESC NULLS LAST
            LIMIT 100
        """)
        user_cards = [{"id": r["id"], "name": r["first_name"] or "",
                       "username": r["username"] or "", "card_count": r["card_count"],
                       "last_update": str(r["last_update"])[:16] if r["last_update"] else None,
                       "has_token": bool(r["has_token"])} for r in cur.fetchall()]

        # All cards detail (+ how many transactions each card has logged)
        cur.execute("""
            SELECT bc.id, bc.user_id, COALESCE(u.first_name, u.username, u.id::text) AS uname,
                   bc.bank, bc.card_last4, bc.alias, bc.balance,
                   bc.auto_log, bc.include_in_balance, bc.created_at, bc.updated_at,
                   (SELECT COUNT(*) FROM transactions t
                     WHERE t.user_id=bc.user_id AND t.account=bc.alias) AS tx_count
            FROM bank_cards bc
            JOIN users u ON u.id = bc.user_id
            ORDER BY bc.created_at DESC NULLS LAST
            LIMIT 200
        """)
        all_cards = [{"id": r["id"], "user_id": r["user_id"], "uname": r["uname"],
                      "bank": r["bank"], "card_last4": r["card_last4"], "alias": r["alias"],
                      "balance": r["balance"], "auto_log": r["auto_log"],
                      "include_in_balance": r["include_in_balance"],
                      "tx_count": r["tx_count"],
                      "created_at": str(r["created_at"])[:10] if r["created_at"] else None,
                      "updated_at": str(r["updated_at"])[:16] if r["updated_at"] else None}
                     for r in cur.fetchall()]

        # ── Forwarding feature usage ──────────────────────────────────────────
        cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE source='forward'")
        total_forwarded = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM transactions WHERE source='forward'")
        forwarders_count = cur.fetchone()["cnt"]

        # Forwarded transactions last 30 days
        cur.execute("""
            SELECT date_str AS day, COUNT(*) AS cnt
            FROM transactions
            WHERE source='forward' AND date_str >= CURRENT_DATE - 30
            GROUP BY date_str ORDER BY date_str
        """)
        forward_daily = [{"day": str(r["day"]), "cnt": r["cnt"]} for r in cur.fetchall()]

        # Per-user forwarding activity (anyone who has forwarded at least once)
        cur.execute("""
            SELECT u.id, COALESCE(u.first_name,'') AS first_name, COALESCE(u.username,'') AS username,
                   COUNT(t.id) AS forwarded_count,
                   MAX(t.date_str::text || ' ' || COALESCE(t.time_str,'')) AS last_forward,
                   (SELECT COUNT(*) FROM bank_cards bc WHERE bc.user_id=u.id) AS card_count
            FROM users u
            JOIN transactions t ON t.user_id=u.id AND t.source='forward'
            GROUP BY u.id, u.first_name, u.username
            ORDER BY forwarded_count DESC
            LIMIT 200
        """)
        forwarding_users = [{"id": r["id"], "name": r["first_name"], "username": r["username"],
                             "forwarded_count": r["forwarded_count"],
                             "last_forward": (r["last_forward"] or "").strip() or None,
                             "card_count": r["card_count"]} for r in cur.fetchall()]

    return {
        "total_cards": total_cards,
        "users_with_cards": users_with_cards,
        "users_without_cards": total_users - users_with_cards,
        "users_with_token": users_with_token,
        "pending_sms": pending_count,
        "auto_log_count": auto_log_count,
        "in_balance_count": in_balance_count,
        "by_bank": by_bank,
        "card_reg": card_reg,
        "user_cards": user_cards,
        "all_cards": all_cards,
        "total_forwarded": total_forwarded,
        "forwarders_count": forwarders_count,
        "forward_daily": forward_daily,
        "forwarding_users": forwarding_users,
    }

# ── Admin: single user full detail ────────────────────────────────────────────
@app.get("/api/admin/users/{user_id}/detail")
def admin_user_detail(user_id: int, token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        cur.execute("""
            SELECT id, type, amount, cat_icon AS cat, cat_name, note, account, date_str
            FROM transactions WHERE user_id=%s ORDER BY date_str DESC, created_at DESC LIMIT 50
        """, (user_id,))
        txs = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT sg.id, sg.name, sg.emoji, sg.target, sg.saved, sg.status, sg.deadline,
                   CASE WHEN sg.creator_id=%s THEN 'creator' ELSE 'partner' END AS role,
                   u2.first_name AS other_name
            FROM shared_goals sg
            LEFT JOIN users u2 ON (CASE WHEN sg.creator_id=%s THEN sg.partner_id ELSE sg.creator_id END = u2.id)
            WHERE sg.creator_id=%s OR sg.partner_id=%s
            ORDER BY sg.created_at DESC
        """, (user_id, user_id, user_id, user_id))
        goals = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT d.id, d.amount, d.debt_type, d.note, d.status, d.created_at,
                   CASE WHEN d.creator_id=%s THEN 'creator' ELSE 'counterpart' END AS role
            FROM social_debts d WHERE d.creator_id=%s OR d.counterpart_id=%s
            ORDER BY d.created_at DESC LIMIT 20
        """, (user_id, user_id, user_id))
        debts = [dict(r) for r in cur.fetchall()]
        # Friends
        cur.execute("""
            SELECT f.id, f.status, f.created_at,
                   CASE WHEN f.requester_id=%s THEN f.addressee_id ELSE f.requester_id END AS friend_id,
                   COALESCE(uf.first_name, uf.username, uf.id::text) AS friend_name,
                   uf.nickname AS friend_nickname, uf.xp AS friend_xp, uf.level AS friend_level
            FROM friends f
            JOIN users uf ON uf.id = (CASE WHEN f.requester_id=%s THEN f.addressee_id ELSE f.requester_id END)
            WHERE f.requester_id=%s OR f.addressee_id=%s
            ORDER BY f.created_at DESC
        """, (user_id, user_id, user_id, user_id))
        friends = [dict(r) for r in cur.fetchall()]
        # Categories
        cur.execute("""
            SELECT id, type, icon, name FROM categories WHERE user_id=%s ORDER BY type, sort_order
        """, (user_id,))
        cats = [dict(r) for r in cur.fetchall()]
    return {
        "user": dict(u),
        "transactions": txs,
        "goals": goals,
        "debts": debts,
        "friends": friends,
        "categories": cats,
    }

# ── Admin: Broadcast ─────────────────────────────────────────────────────────
@app.post("/api/admin/broadcast")
async def admin_broadcast(body: dict, token: str = "", conn=Depends(db)):
    """Send a Telegram message to all users (or a subset)."""
    _admin_auth(token)
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(400, "Message required")
    target = body.get("target", "all")  # "all" | "active7d" | "inactive7d" | "no_nickname"
    specific_ids = body.get("user_ids", [])  # list of specific user IDs
    with_button = body.get("with_button", False)  # add "Open Finly" web_app button
    with conn.cursor() as cur:
        if specific_ids:
            cur.execute("SELECT id FROM users WHERE id = ANY(%s)", (specific_ids,))
        elif target == "no_nickname":
            cur.execute("SELECT id FROM users WHERE nickname IS NULL OR nickname = ''")
        elif target == "active7d":
            cur.execute("SELECT DISTINCT user_id AS id FROM transactions WHERE date_str >= CURRENT_DATE - 7")
        elif target == "inactive7d":
            cur.execute("""
                SELECT u.id FROM users u
                WHERE u.id NOT IN (SELECT DISTINCT user_id FROM transactions WHERE date_str >= CURRENT_DATE - 7)
            """)
        else:
            cur.execute("SELECT id FROM users")
        user_ids = [r["id"] for r in cur.fetchall()]
    kb = {"inline_keyboard": [[{"text": "🚀 Открыть Finly", "web_app": {"url": APP_URL}}]]} if with_button else None
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await send_tg_message(uid, msg, kb)
            sent += 1
        except Exception:
            failed += 1
    return {"sent": sent, "failed": failed, "total": len(user_ids)}

# ── Admin: Send daily reminders ───────────────────────────────────────────────
@app.post("/api/admin/send_reminders")
async def admin_send_reminders(token: str = "", conn=Depends(db)):
    """Send reminder to users who haven't logged today. Can be called by cron."""
    _admin_auth(token)
    return await _do_send_reminders(conn)

async def _do_send_reminders(conn=None):
    close_conn = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.first_name, u.streak, u.xp, u.level,
                       (SELECT COUNT(*) FROM transactions WHERE user_id=u.id AND date_str=CURRENT_DATE) AS today_cnt
                FROM users u
                WHERE u.notifs = true
                AND u.id NOT IN (
                    SELECT DISTINCT user_id FROM transactions WHERE date_str = CURRENT_DATE
                )
            """)
            users = cur.fetchall()
        sent, failed = 0, 0
        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=add"}}]]}

        # Varied greeting templates — picked by user id to ensure each user gets a different one
        GREETINGS = [
            "👋 Привет{name}! Не забудь зафиксировать расходы за сегодня.",
            "💡{name} Как день? Запиши свои траты, пока не забыл.",
            "📝{name} Пара минут — и финансы под контролем. Добавь запись?",
            "🌙{name} День почти закончился. Что потратил сегодня?",
            "✏️{name} Финансовый дневник ждёт твою запись за сегодня.",
            "🎯{name} Маленький шаг — большая привычка. Запиши расходы!",
            "💰{name} Твои деньги хотят быть посчитанными 😄 Добавь запись!",
            "🔔{name} Ежедневный учёт — путь к финансовой свободе. Записал?",
        ]

        for u in users:
            uid    = u["id"]
            fname  = (u["first_name"] or "").strip()
            streak = u["streak"] or 0
            level  = u["level"] or 1
            xp     = u["xp"] or 0
            try:
                insights = []
                with conn.cursor() as cur:
                    # Yesterday's top spending category
                    cur.execute("""
                        SELECT cat_icon, cat_name, SUM(amount) AS total
                        FROM transactions
                        WHERE user_id=%s AND date_str = CURRENT_DATE - 1 AND type='expense'
                        GROUP BY cat_icon, cat_name ORDER BY total DESC LIMIT 1
                    """, (uid,))
                    top = cur.fetchone()

                    # Same category same weekday last week (for comparison)
                    last_week_same = 0
                    if top:
                        cur.execute("""
                            SELECT COALESCE(SUM(amount),0) AS total
                            FROM transactions
                            WHERE user_id=%s AND cat_name=%s AND type='expense'
                              AND date_str = CURRENT_DATE - 8
                        """, (uid, top["cat_name"]))
                        last_week_same = int(cur.fetchone()["total"])

                    # Nearest active goal
                    cur.execute("""
                        SELECT name, emoji, saved, target
                        FROM personal_goals WHERE user_id=%s AND done=FALSE AND target>0
                        ORDER BY (saved::float/target) DESC LIMIT 1
                    """, (uid,))
                    top_goal = cur.fetchone()

                    # Total spent this month
                    cur.execute("""
                        SELECT COALESCE(SUM(amount),0) AS total
                        FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND date_str >= date_trunc('month', CURRENT_DATE)
                    """, (uid,))
                    month_exp = int(cur.fetchone()["total"])

                    # How many days logged this month
                    cur.execute("""
                        SELECT COUNT(DISTINCT date_str) AS days
                        FROM transactions WHERE user_id=%s
                          AND date_str >= date_trunc('month', CURRENT_DATE)
                    """, (uid,))
                    month_days = int(cur.fetchone()["days"])

                # Personalized greeting using uid as seed for variety
                name_part = f", {fname}" if fname else ""
                greeting_tpl = GREETINGS[uid % len(GREETINGS)]
                greeting = greeting_tpl.replace("{name}", name_part)

                # Build context-aware insight lines
                if top:
                    amt = f"{int(top['total']):,}".replace(",", " ")
                    if last_week_same > 0:
                        delta_pct = round((int(top["total"]) - last_week_same) / last_week_same * 100)
                        if delta_pct <= -15:
                            insights.append(f"📉 Вчера на {top['cat_icon']} <b>{top['cat_name']}</b> потратил на <b>{abs(delta_pct)}% меньше</b>, чем неделю назад — отлично!")
                        elif delta_pct >= 25:
                            insights.append(f"📈 Вчера на {top['cat_icon']} <b>{top['cat_name']}</b> ушло на <b>{delta_pct}% больше</b> обычного. Держи под контролем!")
                        else:
                            insights.append(f"Вчера больше всего ушло на {top['cat_icon']} <b>{top['cat_name']}</b> — <b>{amt} сум</b>.")
                    else:
                        insights.append(f"Вчера больше всего ушло на {top['cat_icon']} <b>{top['cat_name']}</b> — <b>{amt} сум</b>.")

                if top_goal:
                    remaining = int(top_goal["target"]) - int(top_goal["saved"])
                    pct = round(int(top_goal["saved"]) / int(top_goal["target"]) * 100)
                    rem_fmt = f"{remaining:,}".replace(",", " ")
                    if pct >= 90:
                        insights.append(f"🎯 Цель «{top_goal['name']}» {top_goal['emoji']} — уже <b>{pct}%</b>! Осталось <b>{rem_fmt} сум</b>. Финишная прямая!")
                    elif pct >= 50:
                        insights.append(f"🎯 Половина пути к цели «{top_goal['name']}» {top_goal['emoji']} — <b>{pct}%</b> выполнено!")

                if month_days >= 20:
                    insights.append(f"⭐ В этом месяце ты записал расходы уже <b>{month_days} дней</b> — почти идеальный результат!")
                elif month_days >= 10 and month_exp > 0:
                    m_fmt = f"{month_exp:,}".replace(",", " ")
                    insights.append(f"📊 В этом месяце: <b>{m_fmt} сум</b> расходов за {month_days} дн.")

                # Streak / level motivation
                if streak >= 7:
                    streak_msg = f"🔥 Серия <b>{streak} дней</b> — это впечатляет{', '+fname if fname else ''}! Не останавливайся."
                elif streak >= 3:
                    streak_msg = f"🔥 Серия <b>{streak} дн.</b> — хорошее начало! Держи темп."
                elif streak == 0:
                    streak_msg = "💡 Начни новую серию сегодня — запиши первую трату!"
                else:
                    streak_msg = f"🔥 Серия: {streak} дн. — не прерывай!"

                body_lines = insights[:2]
                body = "\n\n".join(body_lines) if body_lines else "Запиши свои расходы за сегодня."
                msg = f"{greeting}\n\n{body}\n\n{streak_msg}"
                await send_tg_message(uid, msg, kb)
                sent += 1
            except Exception as e:
                log.warning(f"Reminder uid={uid}: {e}")
                failed += 1
        log.info(f"Daily reminders: sent={sent} failed={failed}")
        return {"sent": sent, "failed": failed}
    finally:
        if close_conn:
            conn.close()

# ── Daily reminder background scheduler ──────────────────────────────────────
async def _reminder_scheduler():
    """Runs forever; fires reminder job every day at 18:00 Tashkent (UTC+5 = 13:00 UTC)."""
    REMINDER_HOUR_UTC = 13  # 18:00 UZT
    while True:
        try:
            now = datetime.utcnow()
            target = now.replace(hour=REMINDER_HOUR_UTC, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await _do_send_reminders()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Reminder scheduler error: {e}")
            await asyncio.sleep(60)

# ── Weekly summary ────────────────────────────────────────────────────────────
async def _do_weekly_summary(conn=None):
    """Send weekly recap every Monday at 09:00 UZT."""
    close_conn = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE weekly_report=true AND notifs=true")
            user_ids = [r["id"] for r in cur.fetchall()]
        sent = 0
        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=analytics"}}]]}
        for uid in user_ids:
            try:
                with conn.cursor() as cur:
                    # This week Mon–Sun
                    cur.execute("""
                        SELECT
                          COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS exp,
                          COALESCE(SUM(CASE WHEN type='income'  THEN amount END),0) AS inc,
                          COUNT(*) AS cnt
                        FROM transactions
                        WHERE user_id=%s
                          AND date_str >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'
                          AND date_str <  date_trunc('week', CURRENT_DATE)
                    """, (uid,))
                    week = cur.fetchone()

                    # Previous week
                    cur.execute("""
                        SELECT COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS exp
                        FROM transactions
                        WHERE user_id=%s
                          AND date_str >= date_trunc('week', CURRENT_DATE) - INTERVAL '14 days'
                          AND date_str <  date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'
                    """, (uid,))
                    prev_exp = cur.fetchone()["exp"] or 0

                    # Top category last week
                    cur.execute("""
                        SELECT cat_icon, cat_name, SUM(amount) AS total
                        FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND date_str >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'
                          AND date_str <  date_trunc('week', CURRENT_DATE)
                        GROUP BY cat_icon, cat_name ORDER BY total DESC LIMIT 1
                    """, (uid,))
                    top_cat = cur.fetchone()

                    # Biggest single expense last week
                    cur.execute("""
                        SELECT cat_icon, cat_name, amount, note
                        FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND date_str >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'
                          AND date_str <  date_trunc('week', CURRENT_DATE)
                        ORDER BY amount DESC LIMIT 1
                    """, (uid,))
                    biggest = cur.fetchone()

                exp = int(week["exp"]); inc = int(week["inc"]); cnt = int(week["cnt"])
                if cnt == 0:
                    continue  # nothing to report

                def fmt(n): return f"{n:,}".replace(",", " ")

                # Per-category comparison: find biggest change vs last week
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT cat_icon, cat_name,
                          SUM(CASE WHEN date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '7 days'
                                    AND date_str <  date_trunc('week',CURRENT_DATE) THEN amount ELSE 0 END) AS this_w,
                          SUM(CASE WHEN date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '14 days'
                                    AND date_str <  date_trunc('week',CURRENT_DATE) - INTERVAL '7 days' THEN amount ELSE 0 END) AS prev_w
                        FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '14 days'
                          AND date_str <  date_trunc('week',CURRENT_DATE)
                        GROUP BY cat_icon, cat_name
                        HAVING SUM(CASE WHEN date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '14 days'
                                    AND date_str < date_trunc('week',CURRENT_DATE) - INTERVAL '7 days' THEN amount ELSE 0 END) > 0
                        ORDER BY (SUM(CASE WHEN date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '7 days'
                                    AND date_str < date_trunc('week',CURRENT_DATE) THEN amount ELSE 0 END) -
                                  SUM(CASE WHEN date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '14 days'
                                    AND date_str < date_trunc('week',CURRENT_DATE) - INTERVAL '7 days' THEN amount ELSE 0 END)) ASC
                        LIMIT 1
                    """, (uid,))
                    best_cat = cur.fetchone()  # most improved category

                    # Days this week without impulse spend
                    cur.execute("""
                        SELECT COUNT(DISTINCT date_str) AS cnt FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND cat_name IN ('Развлечения','Забыл','Прочее')
                          AND date_str >= date_trunc('week',CURRENT_DATE) - INTERVAL '7 days'
                          AND date_str <  date_trunc('week',CURRENT_DATE)
                    """, (uid,))
                    impulse_days = int(cur.fetchone()["cnt"])
                    clean_days = 7 - impulse_days

                    # Goal closest to completion
                    cur.execute("""
                        SELECT name, emoji, saved, target
                        FROM personal_goals WHERE user_id=%s AND done=FALSE AND target>0
                        ORDER BY (saved::float/target) DESC LIMIT 1
                    """, (uid,))
                    top_goal = cur.fetchone()

                lines = [f"📊 <b>Итоги недели</b>\n"]

                # Overall comparison — identity reinforcing phrasing
                if prev_exp > 0:
                    delta = exp - prev_exp
                    pct = abs(round(delta / prev_exp * 100))
                    if delta < 0:
                        lines.append(f"📉 Расходы на <b>{pct}% меньше</b>, чем на прошлой неделе — ты становишься финансово дисциплинированным 💪")
                    elif delta > 0:
                        lines.append(f"📈 Расходы выросли на {pct}% по сравнению с прошлой неделей")
                    else:
                        lines.append(f"💸 Расходы: <b>{fmt(exp)} сум</b>")
                else:
                    lines.append(f"💸 Расходы: <b>{fmt(exp)} сум</b>")

                if inc > 0:
                    lines.append(f"💰 Доходы: <b>{fmt(inc)} сум</b>")
                lines.append(f"📝 Записей: {cnt}")

                # Best improved category
                if best_cat and int(best_cat["this_w"]) < int(best_cat["prev_w"]):
                    saved_amt = int(best_cat["prev_w"]) - int(best_cat["this_w"])
                    cat_pct = round(saved_amt / int(best_cat["prev_w"]) * 100)
                    lines.append(f"\n✨ На {best_cat['cat_icon']} <b>{best_cat['cat_name']}</b> ты потратил на <b>{cat_pct}% меньше</b>, чем на прошлой неделе!")

                # Top spending category
                if top_cat:
                    lines.append(f"🏆 Топ трата: {top_cat['cat_icon']} {top_cat['cat_name']} — {fmt(int(top_cat['total']))} сум")

                if biggest:
                    lines.append(f"💥 Самая крупная: {biggest['cat_icon']} {fmt(int(biggest['amount']))} сум ({biggest['note'] or biggest['cat_name']})")

                # Impulse control
                if clean_days >= 5:
                    lines.append(f"\n💪 <b>{clean_days} из 7 дней</b> без импульсивных трат — отличная неделя!")
                elif clean_days == 7:
                    lines.append(f"\n🏆 <b>Идеальная неделя</b> — ни одной импульсивной траты! Ты контролируешь деньги, а не наоборот.")

                # Goal nudge
                if top_goal:
                    pct_goal = round(int(top_goal["saved"]) / int(top_goal["target"]) * 100)
                    remaining = int(top_goal["target"]) - int(top_goal["saved"])
                    rem_fmt = fmt(remaining)
                    lines.append(f"\n🎯 Ты ближе к цели «{top_goal['name']}» {top_goal['emoji']} — уже <b>{pct_goal}%</b> ({rem_fmt} сум осталось)")

                # Build pie chart of expenses by category
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT cat_icon, cat_name, SUM(amount) AS total
                        FROM transactions
                        WHERE user_id=%s AND type='expense'
                          AND date_str >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days'
                          AND date_str <  date_trunc('week', CURRENT_DATE)
                        GROUP BY cat_icon, cat_name ORDER BY total DESC LIMIT 8
                    """, (uid,))
                    pie_cats = cur.fetchall()

                if pie_cats and len(pie_cats) >= 2:
                    labels = [f"{r['cat_icon']} {r['cat_name']}" for r in pie_cats]
                    values = [int(r['total']) for r in pie_cats]
                    chart_url = _build_pie_chart_url(labels, values, "Расходы по категориям")
                    caption = "\n".join(lines)[:1024]  # Telegram caption limit
                    await send_tg_photo_url(uid, chart_url, caption, kb)
                else:
                    await send_tg_message(uid, "\n".join(lines), kb)
                sent += 1
            except Exception as e:
                log.warning(f"Weekly summary uid={uid}: {e}")
        log.info(f"Weekly summaries sent: {sent}")
        return {"sent": sent}
    finally:
        if close_conn:
            conn.close()

async def _weekly_scheduler():
    """Fire every Monday at 09:00 UZT (04:00 UTC)."""
    while True:
        try:
            now = datetime.utcnow()
            # Next Monday 04:00 UTC
            days_ahead = (7 - now.weekday()) % 7  # days until Monday
            if days_ahead == 0 and now.hour >= 4:
                days_ahead = 7
            target = (now + timedelta(days=days_ahead)).replace(hour=4, minute=0, second=0, microsecond=0)
            await asyncio.sleep((target - now).total_seconds())
            await _do_weekly_summary()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Weekly scheduler error: {e}")
            await asyncio.sleep(3600)

@app.post("/api/admin/send_weekly")
async def admin_send_weekly(token: str = "", conn=Depends(db)):
    """Manually trigger weekly summaries (admin)."""
    _admin_auth(token)
    return await _do_weekly_summary(conn)

# ── Budget alerts ──────────────────────────────────────────────────────────────
async def _check_budget_alerts(uid: int):
    """After a transaction, check if any budget crossed 80% or 100% and notify."""
    try:
        conn = get_conn_with_timeout()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, icon, limit_amt FROM budgets WHERE user_id=%s", (uid,))
                budgets = cur.fetchall()
                if not budgets:
                    return
                # Get current month spending per category
                cur.execute("""
                    SELECT cat_name, SUM(amount) AS spent
                    FROM transactions
                    WHERE user_id=%s AND type='expense'
                      AND date_str >= date_trunc('month', CURRENT_DATE)
                    GROUP BY cat_name
                """, (uid,))
                spent_map = {r["cat_name"]: int(r["spent"]) for r in cur.fetchall()}
        finally:
            conn.close()

        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=analytics"}}]]}
        for b in budgets:
            spent = spent_map.get(b["name"], 0)
            limit = b["limit_amt"]
            if limit <= 0:
                continue
            pct = spent / limit * 100
            def fmt(n): return f"{n:,}".replace(",", " ")
            if pct >= 100:
                await send_tg_message(uid,
                    f"🚨 <b>Бюджет превышен!</b>\n\n"
                    f"{b['icon']} {b['name']}: потрачено <b>{fmt(spent)} сум</b> из {fmt(limit)} сум\n"
                    f"Лимит превышен на <b>{fmt(spent - limit)} сум</b>", kb)
            elif pct >= 80:
                left = limit - spent
                await send_tg_message(uid,
                    f"⚠️ <b>Бюджет заканчивается</b>\n\n"
                    f"{b['icon']} {b['name']}: осталось <b>{fmt(left)} сум</b> ({round(100-pct)}%)\n"
                    f"Потрачено {fmt(spent)} из {fmt(limit)} сум", kb)
    except Exception as e:
        log.warning(f"Budget alert error uid={uid}: {e}")

# ── Leaderboard overtake notifications ────────────────────────────────────────
async def _check_leaderboard_overtake(uid: int, old_xp: int, new_xp: int):
    """If this user just passed someone, notify the overtaken user.
    Respects rank_visible privacy: anonymous/nobody passers are shown as 'Кто-то'.
    Only notifies overtaken users who haven't hidden themselves from the leaderboard.
    """
    try:
        conn = get_conn_with_timeout()
        try:
            with conn.cursor() as cur:
                # Only notify users who are visible on the leaderboard (not hidden)
                # and have notifications enabled
                cur.execute("""
                    SELECT id, first_name, username, nickname, xp
                    FROM users
                    WHERE xp > %s AND xp <= %s AND id != %s
                      AND notifs = true
                      AND notif_overtake = true
                      AND rank_visible NOT IN ('nobody', 'anonymous')
                """, (old_xp, new_xp, uid))
                overtaken = cur.fetchall()
                if not overtaken:
                    return
                # Get passer's name AND their rank_visible setting
                cur.execute(
                    "SELECT first_name, nickname, username, rank_visible FROM users WHERE id=%s",
                    (uid,)
                )
                passer = cur.fetchone()
        finally:
            conn.close()

        if not passer:
            return

        # Respect passer's privacy setting — don't reveal identity if anonymous/nobody
        passer_rv = passer.get("rank_visible", "everyone") or "everyone"
        if passer_rv in ("anonymous", "nobody"):
            passer_name = "Кто-то"
        else:
            passer_name = passer["nickname"] or passer["first_name"] or "Кто-то"

        kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=rewards"}}]]}
        for u in overtaken:
            await send_tg_message(u["id"],
                f"😤 <b>{passer_name} обогнал тебя в рейтинге!</b>\n\n"
                f"Их XP: {new_xp:,} · Твой XP: {u['xp']:,}\n\n"
                f"Записывай больше — вернись на своё место! 💪".replace(",", " "), kb)
    except Exception as e:
        log.warning(f"Leaderboard overtake check error: {e}")

# ── Goal milestone notifications ───────────────────────────────────────────────
async def _check_goal_milestone(uid: int, goal_name: str, emoji: str,
                                 old_saved: int, new_saved: int, target: int):
    """Send a celebration when goal crosses 25 / 50 / 75 / 100%."""
    if target <= 0:
        return
    milestones = [25, 50, 75, 100]
    old_pct = old_saved / target * 100
    new_pct = new_saved / target * 100
    kb = {"inline_keyboard": [[{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL + "?page=profile"}}]]}
    for m in milestones:
        if old_pct < m <= new_pct:
            if m == 100:
                msg = (f"🎉 <b>Цель достигнута!</b>\n\n"
                       f"{emoji} {goal_name}\n"
                       f"Ты накопил всю сумму — <b>молодец!</b> 🏆")
            else:
                msg = (f"🎯 <b>{m}% пути пройдено!</b>\n\n"
                       f"{emoji} {goal_name}\n"
                       f"Продолжай в том же духе, ты на верном пути 💪")
            try:
                await send_tg_message(uid, msg, kb)
            except Exception as e:
                log.warning(f"Goal milestone notification error: {e}")
            break  # only send one milestone at a time

# ── iOS Shortcuts / Direct SMS Webhook ───────────────────────────────────────

@app.get("/api/sms_token")
def get_sms_token(tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Return (or generate) the user's personal SMS webhook token."""
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT sms_token FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        token = row["sms_token"] if row else None
        if not token:
            token = secrets.token_urlsafe(24)
            cur.execute("UPDATE users SET sms_token=%s WHERE id=%s", (token, uid))
            conn.commit()
    webhook_url = f"{APP_URL}/sms/{token}"
    return {"token": token, "webhook_url": webhook_url}

@app.post("/api/sms_token/rotate")
def rotate_sms_token(tg_user: dict = Depends(require_user), conn=Depends(db)):
    """Regenerate the user's SMS token (invalidates old one)."""
    uid = tg_user["id"]
    token = secrets.token_urlsafe(24)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET sms_token=%s WHERE id=%s", (token, uid))
    conn.commit()
    return {"token": token, "webhook_url": f"{APP_URL}/sms/{token}"}

def _b64_decode_safe(s: str) -> str:
    """Decode base64 (standard or URL-safe) with padding fix."""
    s = s.strip().replace(" ", "+")  # iOS sometimes encodes + as space
    # add padding
    rem = len(s) % 4
    if rem:
        s += "=" * (4 - rem)
    try:
        return base64.urlsafe_b64decode(s).decode("utf-8")
    except Exception:
        try:
            return base64.b64decode(s).decode("utf-8")
        except Exception:
            return s  # fallback: treat as raw text

@app.post("/sms/{token}")
async def sms_webhook(token: str, request: Request, conn=Depends(db)):
    """
    Direct SMS webhook — called by iOS Shortcuts or any HTTP client.
    Body: { "text": "<raw SMS text>" }  OR plain text body  OR form field 'text'.
    No Telegram auth required — the token IS the auth.
    """
    # Verify token and get user
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE sms_token=%s", (token,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(403, "Invalid token")
    uid = row["id"]

    ct = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        form = await request.form()
        text = (form.get("text") or "").strip()
    elif "application/json" in ct:
        try:
            body = await request.json()
            text = body.get("text", "").strip()
        except Exception:
            text = ""
    else:
        # plain text or unknown — read raw body
        text = (await request.body()).decode("utf-8", errors="ignore").strip()

    if not text:
        raise HTTPException(400, "text required")
    return await _process_sms(uid, text, conn)

@app.post("/sms/b64/{token}")
@app.get("/sms/b64/{token}")
async def sms_webhook_b64(token: str, request: Request, t: str = "", conn=Depends(db)):
    """
    Base64-encoded SMS webhook — avoids all Unicode/special-char URL encoding issues.
    GET  /sms/b64/{token}?t=BASE64_OF_SMS
    POST /sms/b64/{token}  body = BASE64_OF_SMS (plain text)
    iOS Shortcut: Encode [Messages Body] → Base64 → append to URL or send as body.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE sms_token=%s", (token,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(403, "Invalid token")

    if t:
        text = _b64_decode_safe(t)
    else:
        raw = (await request.body()).decode("utf-8", errors="ignore").strip()
        text = _b64_decode_safe(raw) if raw else ""

    if not text.strip():
        raise HTTPException(400, "text required")
    return await _process_sms(row["id"], text.strip(), conn)

async def _process_sms(uid: int, text: str, conn):
    parsed = _parse_bank_sms(text)
    if not parsed:
        return {"ok": False, "message": "Not a recognized bank SMS"}

    p = parsed
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sms_pending (user_id,raw_text,bank,card_last4,amount,tx_type,merchant,balance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (uid, text[:500], p["bank"], p["card_last4"], p["amount"],
              p["tx_type"], p["merchant"] or "", p["balance"]))
        pending_id = cur.fetchone()["id"]
    conn.commit()

    sign = "−" if p["tx_type"] == "expense" else "+"
    emoji = "💳" if p["tx_type"] == "expense" else "💰"
    card_str = f" *{p['card_last4']}" if p["card_last4"] else ""
    bank_str = f"{p['bank']}{card_str}" if p["bank"] else "Банк"
    merchant_str = p["merchant"] or ("Покупка" if p["tx_type"] == "expense" else "Пополнение")
    bal_str = f"\n💰 Остаток: {p['balance']:,} сум".replace(",", " ") if p["balance"] else ""
    auto_cat = _guess_category(p["merchant"], p["tx_type"])

    msg_text = (
        f"{emoji} <b>{sign}{p['amount']:,} сум</b>\n"
        f"🏪 {merchant_str}\n"
        f"🏦 {bank_str}{bal_str}\n\n"
        f"Категория: {auto_cat['icon']} {auto_cat['name']}\n"
        f"Записать как {'расход' if p['tx_type'] == 'expense' else 'доход'}?"
    ).replace(",", " ")

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Записать", "callback_data": f"sms:confirm:{pending_id}"},
        {"text": "🏷 Категория", "callback_data": f"sms:cat:{pending_id}"},
        {"text": "❌ Нет",       "callback_data": f"sms:cancel:{pending_id}"},
    ]]}
    await send_tg_message(uid, msg_text, keyboard)
    return {"ok": True, "pending_id": pending_id,
            "amount": p["amount"], "bank": p["bank"], "type": p["tx_type"]}

@app.get("/sms/{token}")
async def sms_webhook_get(token: str, text: str = "", conn=Depends(db)):
    """GET /sms/{token}?text=... — for iOS Shortcuts (GET is more reliable than POST on iOS)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE sms_token=%s", (token,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(403, "Invalid token")
    if not text.strip():
        raise HTTPException(400, "text required")
    return await _process_sms(row["id"], text.strip(), conn)

@app.get("/sms/shortcut/{token}")
async def download_shortcut(token: str, conn=Depends(db)):
    """
    Returns the step-by-step HTML guide for setting up the iOS Shortcut.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE sms_token=%s", (token,))
        if not cur.fetchone():
            raise HTTPException(403, "Invalid token")
    b64_url = f"{APP_URL}/sms/b64/{token}?t="
    icloud_link = "https://www.icloud.com/shortcuts/7eb545ca906a44668341ea34575ac487"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finly — настройка SMS</title>
<style>
body{{font-family:-apple-system,sans-serif;background:#000;color:#fff;padding:24px;max-width:480px;margin:0 auto;}}
.card{{background:#1c1c1e;border-radius:16px;padding:20px;margin-bottom:16px;}}
h1{{font-size:22px;font-weight:800;margin-bottom:4px;}}
.sub{{font-size:14px;color:#888;margin-bottom:24px;}}
.step{{display:flex;gap:14px;margin-bottom:18px;align-items:flex-start;}}
.num{{width:28px;height:28px;border-radius:50%;background:#6366F1;color:#fff;font-weight:800;font-size:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px;}}
.step-text{{font-size:14px;line-height:1.7;color:#ccc;}}
.step-text b{{color:#fff;}}
.url-box{{background:#0a0a0a;border:1px solid #333;border-radius:10px;padding:12px 14px;font-family:monospace;font-size:11px;color:#818CF8;word-break:break-all;margin:10px 0;user-select:all;-webkit-user-select:all;}}
.btn{{display:block;text-align:center;padding:16px;border-radius:14px;font-weight:800;font-size:16px;text-decoration:none;margin-bottom:10px;}}
.btn-purple{{background:#6366F1;color:#fff;}}
.note{{font-size:12px;color:#555;margin-top:6px;line-height:1.6;}}
</style></head><body>
<h1>🍎 Finly для iOS</h1>
<p class="sub">Автоматическое логирование банковских SMS — 3 шага</p>

<div class="card">
  <div class="step">
    <div class="num">1</div>
    <div class="step-text">
      <b>Установите быструю команду Finly</b><br>
      <a class="btn btn-purple" href="{icloud_link}" style="margin-top:12px;display:block;">Установить быструю команду</a>
      <div class="note">Нажмите кнопку выше → откроется Быстрые команды → нажмите <b>Добавить команду</b></div>
    </div>
  </div>

  <div class="step">
    <div class="num">2</div>
    <div class="step-text">
      <b>Вставьте ваш личный URL в команду</b><br>
      Откройте команду <b>Finly SMS</b> → найдите действие <b>Получить содержимое URL</b> → замените URL на:<br>
      <div class="url-box">{b64_url}</div>
      <div class="note">Нажмите и удерживайте текст выше, чтобы скопировать. После вставки добавьте переменную <b>Base64 Encoded</b> в конец URL.</div>
    </div>
  </div>

  <div class="step">
    <div class="num">3</div>
    <div class="step-text">
      <b>Создайте автоматизацию</b><br><br>
      1. Откройте <b>Быстрые команды</b> → вкладка <b>Автоматизации</b><br>
      2. Нажмите <b>+</b> → <b>Новая автоматизация</b><br>
      3. Выберите <b>Сообщение</b> → в поле <b>Отправитель</b> укажите ваш банк (UzCard, Kapitalbank и т.д.) → <b>Далее</b><br>
      4. Нажмите <b>Добавить действие</b> → найдите и выберите команду <b>Finly SMS</b><br>
      5. Переключите <b>Спрашивать перед запуском</b> на <b>Запустить сразу</b> → <b>Готово</b>
    </div>
  </div>
</div>

<div class="card">
  <div style="font-size:13px;color:#888;line-height:1.8;">
    ✅ Готово! Каждое банковское SMS будет автоматически появляться в боте для подтверждения<br>
    🔒 Ваш URL — личный. Не передавайте его другим
  </div>
</div>
</body></html>""")

@app.get("/sms/install/{token}")
async def install_shortcut_file(token: str, conn=Depends(db)):
    """
    Returns a .shortcut plist file pre-configured with the user's webhook URL.
    When opened on iOS, the Shortcuts app imports it automatically.
    """
    import plistlib
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE sms_token=%s", (token,))
        if not cur.fetchone():
            raise HTTPException(403, "Invalid token")
    webhook_url = f"{APP_URL}/sms/{token}"

    # Build the shortcut plist:
    # Action: Get Contents of URL (POST JSON {"text": <ShortcutInput>})
    action_uuid = str(uuid.uuid4()).upper()
    plist_data = {
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowClientVersion": "1300.0.0",
        "WFWorkflowName": "Finly SMS",
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowTypes": [],
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 4282601983,
            "WFWorkflowIconGlyphNumber": 59511,
        },
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
                "WFWorkflowActionParameters": {
                    "UUID": action_uuid,
                    "WFHTTPMethod": "POST",
                    "WFURL": webhook_url,
                    "WFHTTPBodyType": "JSON",
                    "WFJSONValues": {
                        "Value": {
                            "WFDictionaryFieldValueItems": [
                                {
                                    "WFItemType": 0,
                                    "WFKey": {
                                        "Value": {"string": "text"},
                                        "WFSerializationType": "WFTextTokenString",
                                    },
                                    "WFValue": {
                                        "Value": {
                                            "attachmentsByRange": {
                                                "{0, 1}": {
                                                    "Aggrandizements": [],
                                                    "Type": "ExtensionInput",
                                                }
                                            },
                                            "string": "￼",
                                        },
                                        "WFSerializationType": "WFTextTokenString",
                                    },
                                }
                            ]
                        },
                        "WFSerializationType": "WFDictionaryFieldValue",
                    },
                },
            }
        ],
    }

    plist_bytes = plistlib.dumps(plist_data, fmt=plistlib.FMT_XML)

    from fastapi.responses import Response
    return Response(
        content=plist_bytes,
        media_type="application/vnd.apple.shortcut",
        headers={
            "Content-Disposition": f'attachment; filename="Finly SMS.shortcut"',
            "Cache-Control": "no-cache",
        },
    )

# ── Bank Cards API ────────────────────────────────────────────────────────────
BANK_COLORS = {
    "Uzcard":     "#1E3A5F",
    "HUMO":       "#7C3AED",
    "Kapitalbank":"#0F766E",
    "Hamkorbank": "#B45309",
    "TBC":        "#0369A1",
    "Alif":       "#047857",
    "MyUzcard":   "#4338CA",
}

@app.get("/api/bank_cards")
def get_bank_cards(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bank_cards WHERE user_id=%s ORDER BY created_at", (uid,))
        rows = cur.fetchall()
    return [{"id":r["id"],"bank":r["bank"],"card_last4":r["card_last4"],
             "alias":r["alias"],"balance":r["balance"],"currency":r["currency"],
             "auto_log":r["auto_log"],"include_in_balance":r.get("include_in_balance",True),"color":r["color"]} for r in rows]

@app.post("/api/bank_cards")
def add_bank_card(body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    bank = body.get("bank","").strip()
    alias = body.get("alias","").strip() or bank
    card_last4 = re.sub(r'\D', '', body.get("card_last4",""))[-4:]
    balance = int(body.get("balance",0))
    color = BANK_COLORS.get(bank, body.get("color","#6366F1"))
    include_in_balance = bool(body.get("include_in_balance", True))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bank_cards (user_id,bank,card_last4,alias,balance,color,auto_log,include_in_balance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (uid, bank, card_last4, alias, balance, color, True, include_in_balance))
        row = cur.fetchone()
    conn.commit()
    return {"id":row["id"],"bank":bank,"card_last4":card_last4,"alias":alias,
            "balance":balance,"color":color,"auto_log":True,"include_in_balance":include_in_balance}

@app.patch("/api/bank_cards/{card_id}")
def update_bank_card(card_id: int, body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    fields, vals = [], []
    for col in ("alias","balance","auto_log","color","include_in_balance"):
        if col in body:
            fields.append(f"{col}=%s"); vals.append(body[col])
    if not fields: raise HTTPException(400,"Nothing to update")
    vals += [card_id, uid]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE bank_cards SET {','.join(fields)},updated_at=NOW() WHERE id=%s AND user_id=%s RETURNING *", vals)
        row = cur.fetchone()
    if not row: raise HTTPException(404,"Card not found")
    conn.commit()
    return {"id":row["id"],"balance":row["balance"],"auto_log":row["auto_log"]}

@app.delete("/api/bank_cards/{card_id}")
def delete_bank_card(card_id: int, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bank_cards WHERE id=%s AND user_id=%s RETURNING id", (card_id, uid))
        if not cur.fetchone(): raise HTTPException(404,"Card not found")
    conn.commit()
    return {"deleted": card_id}

# ── Donations ─────────────────────────────────────────────────────────────────
# Users pledge a monthly amount, pay P2P off-platform (Click/Payme/card
# transfer directly to the developer), then self-report in-app. Self-reports
# are auto-confirmed immediately (trust-based, by design — see project memory
# "donation-tracking-design"); admin can decline a specific row later as the
# safety valve if a report turns out to be false.

def _donation_streak(periods: set[str]) -> int:
    """Consecutive confirmed-donation months, counting back from the current
    month. If the current month hasn't been paid yet, counts back from last
    month instead (grace period), mirroring the daily-streak UX elsewhere."""
    def prev_period(p: str) -> str:
        y, m = int(p[:4]), int(p[5:7])
        return f"{y-1}-12" if m == 1 else f"{y}-{m-1:02d}"
    today_period = date.today().strftime("%Y-%m")
    start = today_period if today_period in periods else prev_period(today_period)
    streak = 0
    cursor = start
    while cursor in periods:
        streak += 1
        cursor = prev_period(cursor)
    return streak

def _donation_summary(conn, uid: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT monthly_amount, active FROM donation_pledges WHERE user_id=%s", (uid,))
        pledge = cur.fetchone()
        cur.execute("""
            SELECT period, amount FROM donations
            WHERE user_id=%s AND status='confirmed' ORDER BY period DESC LIMIT 24
        """, (uid,))
        history = cur.fetchall()
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS total FROM donations
            WHERE user_id=%s AND status='confirmed'
        """, (uid,))
        lifetime_total = cur.fetchone()["total"]
    periods = {r["period"] for r in history}
    today_period = date.today().strftime("%Y-%m")
    return {
        "pledge_amount":   pledge["monthly_amount"] if pledge else 0,
        "pledge_active":   pledge["active"] if pledge else False,
        "paid_this_month": today_period in periods,
        "streak":          _donation_streak(periods),
        "lifetime_total":  lifetime_total,
        "history":         [{"period": r["period"], "amount": r["amount"]} for r in history],
    }

@app.get("/api/donations/me")
def get_my_donations(tg_user: dict = Depends(require_user), conn=Depends(db)):
    return _donation_summary(conn, tg_user["id"])

@app.post("/api/donations/pledge")
def set_donation_pledge(body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    amount = max(0, int(body.get("monthly_amount", 0)))
    active = bool(body.get("active", True))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO donation_pledges (user_id, monthly_amount, active)
            VALUES (%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE
                SET monthly_amount=EXCLUDED.monthly_amount, active=EXCLUDED.active, updated_at=NOW()
        """, (uid, amount, active))
    conn.commit()
    return _donation_summary(conn, uid)

@app.post("/api/donations/report")
async def report_donation(body: dict, tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    amount = int(body.get("amount", 0))
    if amount < 100:
        raise HTTPException(400, "Amount too small")
    note = (body.get("note") or "").strip()[:500]
    period = date.today().strftime("%Y-%m")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO donations (user_id, amount, period, status, note)
            VALUES (%s,%s,%s,'confirmed',%s) RETURNING id
        """, (uid, amount, period, note))
        cur.execute("""
            SELECT period FROM donations WHERE user_id=%s AND status='confirmed'
        """, (uid,))
        periods = {r["period"] for r in cur.fetchall()}
    conn.commit()
    streak = _donation_streak(periods)
    check_achievements(conn, uid, extra_checks={
        "donor1":  True,
        "donor3":  streak >= 3,
        "donor12": streak >= 12,
    })
    conn.commit()
    summary = _donation_summary(conn, uid)
    streak_line = f"\n🎗 {streak} {'месяц' if streak==1 else 'месяца' if streak<5 else 'месяцев'} подряд!" if streak >= 2 else ""
    await send_tg_message(uid,
        f"💜 Спасибо за пожертвование <b>{amount:,}</b> сум!".replace(",","ㅤ") +
        streak_line +
        f"\n\nВсего от тебя: <b>{summary['lifetime_total']:,}</b> сум".replace(",","ㅤ") +
        "\n\nВся сумма идёт напрямую на благотворительность 🙏")
    return summary

@app.get("/api/donations/stats")
def donation_stats(tg_user: dict = Depends(require_user), conn=Depends(db)):
    uid = tg_user["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM donations WHERE status='confirmed'")
        lifetime_total = cur.fetchone()["total"]
        this_period = date.today().strftime("%Y-%m")
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS total FROM donations
            WHERE status='confirmed' AND period=%s
        """, (this_period,))
        this_month_total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM donations WHERE status='confirmed'")
        donor_count = cur.fetchone()["cnt"]

        # Friend IDs for "friends-only" leaderboard visibility (mirrors /api/leaderboard)
        cur.execute("""
            SELECT CASE WHEN requester_id=%s THEN addressee_id ELSE requester_id END AS friend_id
            FROM friends WHERE (requester_id=%s OR addressee_id=%s) AND status='accepted'
        """, (uid, uid, uid))
        friend_ids = {r["friend_id"] for r in cur.fetchall()}

        cur.execute("""
            SELECT u.id, u.nickname, u.rank_visible, SUM(d.amount) AS total
            FROM donations d JOIN users u ON u.id = d.user_id
            WHERE d.status='confirmed'
            GROUP BY u.id, u.nickname, u.rank_visible
            ORDER BY total DESC LIMIT 10
        """)
        top_rows = cur.fetchall()

    top_donors = []
    for r in top_rows:
        is_you = r["id"] == uid
        rv = (r["rank_visible"] or "everyone")
        is_anon = (rv in ("nobody", "anonymous") and not is_you) or \
                  (rv == "friends" and r["id"] not in friend_ids and not is_you)
        top_donors.append({
            "name": "Аноним" if is_anon else (r["nickname"] or "Аноним"),
            "total": r["total"], "is_you": is_you,
        })
    return {
        "lifetime_total": lifetime_total,
        "this_month_total": this_month_total,
        "donor_count": donor_count,
        "top_donors": top_donors,
    }

@app.get("/api/admin/donations")
def admin_list_donations(token: str = "", conn=Depends(db)):
    _admin_auth(token)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id, d.user_id, u.first_name, u.username, d.amount, d.period,
                   d.status, d.note, d.created_at
            FROM donations d JOIN users u ON u.id = d.user_id
            ORDER BY d.created_at DESC LIMIT 500
        """)
        donations = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT p.user_id, u.first_name, u.username, p.monthly_amount, p.active, p.created_at
            FROM donation_pledges p JOIN users u ON u.id = p.user_id
            ORDER BY p.created_at DESC
        """)
        pledges = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM donations WHERE status='confirmed'")
        lifetime_total = cur.fetchone()["total"]
    return {"donations": donations, "pledges": pledges, "lifetime_total": lifetime_total}

@app.patch("/api/admin/donations/{donation_id}")
def admin_update_donation(donation_id: int, body: dict, token: str = "", conn=Depends(db)):
    """Safety valve: decline a self-reported donation that turned out to be false."""
    _admin_auth(token)
    status = body.get("status")
    if status not in ("confirmed", "declined"):
        raise HTTPException(400, "status must be confirmed or declined")
    with conn.cursor() as cur:
        cur.execute("UPDATE donations SET status=%s WHERE id=%s RETURNING id", (status, donation_id))
        if not cur.fetchone(): raise HTTPException(404, "Donation not found")
    conn.commit()
    return {"id": donation_id, "status": status}

# ── iOS Quick-Log Shortcut ────────────────────────────────────────────────────
# Flow: shortcut asks amount → picks category from list → POST → saves tx + Telegram msg
#
# POST /shortcut/log  body: {token, amount, cat_icon, cat_name, note?}
# GET  /shortcut/setup/<token>  → HTML setup guide

# Category list sent to the shortcut (displayed as Choose from List)
SHORTCUT_CATS = [
    ("🛒","Продукты"),("☕","Кафе/Рестораны"),("🚌","Транспорт"),("📱","Связь"),
    ("🏠","Аренда"),("💊","Здоровье"),("👕","Одежда"),("🎉","Развлечения"),
    ("⚡","Коммуналка"),("💵","Зарплата/Доход"),("💬","Прочее"),
]

@app.get("/shortcut/cats")
async def shortcut_cats(conn=Depends(db)):
    """Return hardcoded category list for iOS Shortcut Choose-from-list action."""
    from fastapi.responses import JSONResponse
    return JSONResponse([f"{icon} {name}" for icon, name in SHORTCUT_CATS])

@app.get("/shortcut/log")
async def ios_shortcut_log(
    token: str = "", amount: str = "0", cat: str = "", note: str = "",
    conn=Depends(db)
):
    """
    iOS Shortcut GET endpoint: ?token=X&amount=50000&cat=☕+Кафе
    Saves transaction and sends Telegram confirmation message.
    Returns plain text shown in shortcut notification.
    """
    token       = token.strip()
    cat_raw     = cat.strip()
    note        = note.strip()[:80]
    amount_raw  = amount.strip().lstrip("+")   # strip any accidental + prefix
    try:
        amount = int(float(amount_raw.replace(",","").replace(" ","")))
    except Exception:
        amount = 0

    if not token:
        return PlainTextResponse("❌ Токен не указан. Обновите ярлык.")

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE sms_token=%s", (token,))
        user = cur.fetchone()
    if not user:
        return PlainTextResponse("❌ Неверный токен. Откройте Finly → Карты и обновите токен.")
    if amount <= 0:
        return PlainTextResponse("❌ Сумма должна быть больше нуля.")

    # Parse category — iOS may only send the emoji if the space wasn't encoded
    parts    = cat_raw.split(" ", 1)
    cat_icon = parts[0].strip() if parts else "💬"
    cat_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
    # If name is missing, look it up from the canonical list by emoji
    if not cat_name:
        cat_name = next((n for ic, n in SHORTCUT_CATS if ic == cat_icon), "")
    # Final fallback
    if not cat_icon:
        cat_icon = "💬"
    if not cat_name:
        cat_name = "Прочее"
    tx_type  = "expense"  # FinlyFast always logs expenses (simpler UX)
    if not note:
        note = cat_name

    uid       = user["id"]
    today_str = date.today().isoformat()
    now_str   = datetime.now().strftime("%H:%M")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,account,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS','Наличка','shortcut') RETURNING id
        """, (uid, tx_type, cat_icon, cat_name, note, amount, today_str, now_str))
        cur.execute("UPDATE users SET xp=xp+10 WHERE id=%s", (uid,))
        lld = user.get("last_log_date")
        if lld == date.today():
            pass
        elif lld and (date.today() - lld).days == 1:
            cur.execute("UPDATE users SET streak=streak+1,streak_record=GREATEST(streak_record,streak+1),last_log_date=%s WHERE id=%s", (today_str, uid))
        else:
            cur.execute("UPDATE users SET streak=1,streak_record=GREATEST(streak_record,1),last_log_date=%s WHERE id=%s", (today_str, uid))
        cur.execute("SELECT streak FROM users WHERE id=%s", (uid,))
        streak = (cur.fetchone() or {}).get("streak", 1)
    conn.commit()

    # Compute new balance after this transaction
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE -amount END), 0) AS balance
            FROM transactions WHERE user_id=%s
        """, (uid,))
        new_balance = int((cur.fetchone() or {}).get("balance", 0))

    amt_fmt     = f"{amount:,}".replace(",", " ")
    bal_fmt     = f"{new_balance:,}".replace(",", " ")
    result      = f"✅ {cat_icon} {cat_name}  −{amt_fmt} сум\n💳 Баланс: {bal_fmt} сум"

    # Send Telegram confirmation
    kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
    await send_tg_message(uid,
        f"⚡ <b>Быстрая запись!</b>\n\n"
        f"{cat_icon} <b>{cat_name}</b>\n"
        f"<b>−{amt_fmt} сум</b>\n\n"
        f"💳 Баланс: <b>{bal_fmt} сум</b>\n"
        f"🔥 Серия: {streak} дн.  ·  ⭐ +10 XP", kb)

    return PlainTextResponse(result)

SHORTCUT_ICLOUD = "https://www.icloud.com/shortcuts/eac5141957c34c32acebdc3becd13bc6"

@app.get("/shortcut/setup/{token}")
async def ios_shortcut_setup(token: str, conn=Depends(db)):
    """HTML setup guide for iOS Quick-Log shortcut."""
    from fastapi.responses import HTMLResponse
    with conn.cursor() as cur:
        cur.execute("SELECT id,first_name FROM users WHERE sms_token=%s", (token,))
        user = cur.fetchone()
    if not user:
        return HTMLResponse("<h1>❌ Неверный токен</h1>", status_code=403)
    log_url  = f"{APP_URL}/shortcut/log"
    full_url = f"{log_url}?token={token}&amount="
    name      = user.get("first_name","")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finly Fast — настройка</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:-apple-system,sans-serif;background:#000;color:#fff;padding:20px;max-width:480px;margin:0 auto;}}
h1{{font-size:22px;font-weight:800;margin-bottom:4px;}}
.sub{{font-size:14px;color:#888;margin-bottom:20px;line-height:1.5;}}
.card{{background:#1c1c1e;border-radius:18px;padding:18px;margin-bottom:12px;}}
.step-row{{display:flex;gap:12px;margin-bottom:14px;align-items:flex-start;}}
.step-row:last-child{{margin-bottom:0;}}
.num{{width:28px;height:28px;min-width:28px;border-radius:50%;background:#6366F1;color:#fff;font-weight:800;font-size:12px;display:flex;align-items:center;justify-content:center;margin-top:2px;flex-shrink:0;}}
.step-text{{font-size:14px;line-height:1.6;color:#bbb;}}
.step-text b{{color:#fff;}}
.url-box{{background:#0d0d0f;border:1.5px solid #6366F1;border-radius:12px;padding:12px 14px;font-family:monospace;font-size:12px;color:#818CF8;word-break:break-all;margin:10px 0;line-height:1.6;}}
.copy-btn{{display:block;text-align:center;padding:13px;border-radius:12px;font-weight:800;font-size:14px;background:#6366F1;color:#fff;cursor:pointer;margin-top:6px;border:none;width:100%;-webkit-appearance:none;}}
.copy-btn:active{{opacity:.75;}}
.install-btn{{display:block;text-align:center;padding:15px;border-radius:14px;font-weight:800;font-size:16px;background:linear-gradient(135deg,#6366F1,#8B5CF6);color:#fff;text-decoration:none;margin-bottom:12px;}}
.note{{font-size:12px;color:#555;margin-top:6px;line-height:1.5;}}
.green{{color:#4ADE80;}}
</style>
<script>
function copyURL(id, url, btnId){{
  navigator.clipboard.writeText(url).then(()=>{{
    const btn = document.getElementById(btnId);
    const orig = btn.textContent;
    btn.textContent = '✅ Скопировано!';
    btn.style.background = '#059669';
    setTimeout(()=>{{ btn.textContent=orig; btn.style.background='#6366F1'; }}, 2000);
  }}).catch(()=>{{
    const box = document.getElementById(id);
    const range = document.createRange();
    range.selectNode(box);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    document.execCommand('copy');
  }});
}}
</script>
</head><body>

<h1>⚡ FinlyFast</h1>
<p class="sub">{('Привет, '+name+'! ') if name else ''}2 секунды — и расход записан. Работает с виджетом и кнопкой Action.</p>

<div class="card" style="background:linear-gradient(135deg,#1a1a2e,#1c1c2e);border:1px solid #6366F140;">
  <div style="font-size:12px;color:#818CF8;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px;">Как работает</div>
  <div style="font-size:13px;color:#bbb;line-height:2.2;">
    🔘 Нажал виджет / Action Button<br>
    💬 Ввёл сумму (например: <b style="color:#fff">50000</b>)<br>
    📂 Выбрал категорию из списка<br>
    <span class="green">✅ Готово — запись в Finly + уведомление в Telegram</span>
  </div>
</div>

<a class="install-btn" href="{SHORTCUT_ICLOUD}">📲 Установить ярлык FinlyFast</a>

<div class="card">
  <div style="font-size:12px;color:#818CF8;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;">После установки — 1 шаг</div>
  <div class="step-row">
    <div class="num">1</div>
    <div class="step-text">
      Откройте установленный ярлык <b>FinlyFast</b> → нажмите на <b>ВАШ_ТОКЕН</b> в 4-м действии → <b>замените на ваш URL:</b>
    </div>
  </div>
  <div id="urlbox" class="url-box">{full_url}</div>
  <button id="copybtn" class="copy-btn" onclick="copyURL('urlbox','{full_url}','copybtn')">📋 Скопировать мой URL</button>
  <div class="note">Вставьте этот URL вместо «ВАШ_ТОКЕН» в 4-м действии ярлыка. После <code>amount=</code> должна идти переменная <b>«Ввод»</b>, затем <code>&amp;cat=</code> и <b>«Выбранный элемент»</b> — они уже стоят в шаблоне.</div>
</div>

<div class="card">
  <div style="font-size:12px;color:#818CF8;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;">Добавить на экран</div>
  <div class="step-row">
    <div class="num">A</div>
    <div class="step-text"><b>Виджет:</b> удерживай рабочий стол → «+» → Быстрые команды → выбери FinlyFast</div>
  </div>
  <div class="step-row">
    <div class="num">B</div>
    <div class="step-text"><b>Action Button</b> (iPhone 15 Pro / 16): Настройки → Action Button → Быстрые команды → FinlyFast</div>
  </div>
</div>

<div class="card" style="font-size:13px;color:#555;line-height:2;">
  🔒 Ваш URL персональный — не передавайте его<br>
  📲 После каждой записи придёт уведомление в Telegram<br>
  🔥 Серия и XP сохраняются
</div>
</body></html>""")

# ── SMS Parsing Engine ─────────────────────────────────────────────────────────
def _clean_amount(s: str) -> int | None:
    """Convert various number formats to integer UZS amount.
    Handles: '85 000', '85,000', '85000.50', '15.000,00' (European), '150.650,00',
    and crucially the European decimal comma: '1 000,00' = 1000 (NOT 100000).
    Strategy: detect & drop the trailing decimal part first, then strip all
    remaining separators (spaces/dots/commas) as thousands separators.
    """
    s = s.strip()
    if re.search(r',\d{1,2}$', s):
        # European decimal comma: 1000,00 · 1 000,00 · 250.500,00 → drop ",dd"
        s = s[:s.rfind(',')]
        s = re.sub(r'[ .]', '', s)
    elif re.search(r'\.\d{1,2}$', s) and not re.search(r'\.\d{3}', s):
        # US decimal dot: 85000.50 · 1,234.56 → drop ".dd"
        s = s[:s.rfind('.')]
        s = re.sub(r'[ ,]', '', s)
    else:
        # No decimal part — every space/dot/comma is a thousands separator
        s = re.sub(r'[ .,]', '', s)
    try:
        v = int(s)
        return v if v >= 100 else None
    except:
        return None

def _parse_bank_sms(text: str) -> dict | None:
    """Parse Uzbek bank SMS and return transaction dict or None."""
    t = re.sub(r'\s+', ' ', text.strip())

    DEBIT_KW  = r'(?:списан[оие]|оплат[аы]|покупк[аи]|расход|дебет|платёж|withdraw|debit|xarid|to\'lov|-\s*\d)'
    CREDIT_KW = r'(?:зачислен[оие]|пополнен[ие]|поступил|кредит|deposit|credit|o\'tkazma|kirim|tushum|\+\s*\d)'
    BALANCE_KW = r'(?:остаток|баланс|balance|остат|qoldiq|qoldigi)'

    def get_balance(s):
        m = re.search(BALANCE_KW + r'[:\s]*([\d\s,.]+)', s, re.I)
        return _clean_amount(m.group(1)) if m else None

    def get_merchant(s, after_pattern):
        m = re.search(after_pattern + r'[.\s]+([^.\n]+?)(?:\.|' + BALANCE_KW + r'|$)', s, re.I)
        return m.group(1).strip()[:40] if m else ""

    # ── Uzcard ─────────────────────────────────────────────────────────────────
    if re.search(r'(?i)\buz\s*card\b', t):
        card_m = re.search(r'\*(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        # Format: "UzCard *1234: -85 000 sum. Magnit. Остаток: 1 200 000"
        amt_m = re.search(r'[-]?\s*([\d][\d\s,.]{2,})\s*(?:sum|сум|uzs)', t, re.I)
        if not amt_m:
            amt_m = re.search(DEBIT_KW + r'[:\s]*([\d][\d\s,.]{2,})', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            merchant = get_merchant(t, r'(?:sum|сум)')
            return {"bank":"Uzcard","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":merchant,"balance":get_balance(t)}

    # ── HUMOcardbot emoji format ───────────────────────────────────────────────
    # Format: 🎉 Пополнение ➕ 15.000,00 UZS 📍 MERCHANT 💳 HUMOCARD *0503 💰 150.650,00 UZS
    # Also handles space-thousands: ➖ 1 000,00 UZS
    if re.search(r'(?i)humocard', t) or (re.search(r'[➕➖]', t) and re.search(r'💳', t)):
        card_m = re.search(r'\*(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        # Allow spaces inside the number (thousands separator)
        amt_m = re.search(r'[➕➖]\s*([\d][\d .,]*)\s*(?:uzs|сум|sum)', t, re.I)
        if not amt_m:
            amt_m = re.search(r'([\d][\d .,]*)\s*(?:uzs|сум|sum)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(r'➕|пополнен|зачислен|kirim|tushum', t, re.I) else "expense"
            merchant_m = re.search(r'📍\s*(.+?)(?:\s*💳|\s*🕓|\s*💰|$)', t)
            merchant = merchant_m.group(1).strip()[:40] if merchant_m else ""
            bal_m = re.search(r'💰\s*([\d][\d .,]*)\s*(?:uzs|сум|sum)', t, re.I)
            balance = _clean_amount(bal_m.group(1)) if bal_m else None
            return {"bank":"HUMO","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":merchant,"balance":balance}

    # ── HUMO (plain SMS) ───────────────────────────────────────────────────────
    if re.search(r'(?i)\bhumo', t):
        card_m = re.search(r'\*(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        amt_m = re.search(DEBIT_KW + r'[:\s]*([\d][\d\s,.]{2,})', t, re.I)
        if not amt_m:
            amt_m = re.search(r'([\d][\d\s,.]{2,})\s*(?:sum|сум|uzs)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            merchant = get_merchant(t, r'(?:sum|сум|\*\d{4})')
            return {"bank":"HUMO","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":merchant,"balance":get_balance(t)}

    # ── Kapitalbank / Kapital24 ─────────────────────────────────────────────────
    # Format: "To'lov: FRB KAPITAL 24 MP, UZ,22.05.26 13:10,karta ***4471. Miqdor:3030.00 UZS Qoldiq:56781.54 UZS"
    if re.search(r'(?i)(kapital|kapital\s*24|kapitalbank)', t):
        card_m = re.search(r'\*+(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        amt_m = re.search(r'(?:miqdor|summa)[:\s]*([\d][\d\s,.]+?)\s*(?:uzs|сум|sum)\b', t, re.I)
        if not amt_m:
            amt_m = re.search(r'([\d][\d\s,.]{2,})\s*(?:uzs|сум|sum)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            # Extract merchant: text between action keyword and ", UZ"
            return {"bank":"Kapitalbank","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":"","balance":get_balance(t)}

    # ── Hamkorbank ─────────────────────────────────────────────────────────────
    if re.search(r'(?i)hamkor', t):
        card_m = re.search(r'\*+(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        # Format: "Miqdor:1000.00 UZS Qoldiq:56811.54 UZS"
        amt_m = re.search(r'(?:miqdor|summa)[:\s]*([\d][\d\s,.]+?)\s*(?:uzs|сум|sum)\b', t, re.I)
        if not amt_m:
            amt_m = re.search(r'([\d][\d\s,.]+?)\s*(?:uzs|сум|sum)\b', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            return {"bank":"Hamkorbank","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":"","balance":get_balance(t)}

    # ── TBC Bank UZ ────────────────────────────────────────────────────────────
    if re.search(r'(?i)\btbc\b', t):
        card_m = re.search(r'\*(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        amt_m = re.search(r'([\d][\d\s,.]{2,})\s*(?:uzs|сум|sum)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            return {"bank":"TBC","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":get_merchant(t, r'(?:uzs|сум)'),"balance":get_balance(t)}

    # ── Alif Bank ──────────────────────────────────────────────────────────────
    if re.search(r'(?i)\balif\b', t):
        card_m = re.search(r'\*(\d{4})', t)
        card_last4 = card_m.group(1) if card_m else ""
        amt_m = re.search(r'([\d][\d\s,.]{2,})\s*(?:uzs|сум|sum)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if not amt: return None
            tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
            return {"bank":"Alif","card_last4":card_last4,"amount":amt,
                    "tx_type":tx_type,"merchant":get_merchant(t, r'(?:uzs|сум)'),"balance":get_balance(t)}

    # ── Generic (any message with amount + сум/sum) ────────────────────────────
    # Only match if there's a clear debit/credit keyword to avoid false positives
    if re.search(DEBIT_KW + '|' + CREDIT_KW, t, re.I):
        amt_m = re.search(r'([\d][\d .,]{2,})\s*(?:сум|sum|uzs)', t, re.I)
        if amt_m:
            amt = _clean_amount(amt_m.group(1))
            if amt and amt >= 500:
                tx_type = "income" if re.search(CREDIT_KW, t, re.I) else "expense"
                return {"bank":"","card_last4":"","amount":amt,
                        "tx_type":tx_type,"merchant":"","balance":get_balance(t)}
    return None

def _guess_category(merchant: str, tx_type: str) -> dict:
    """Auto-categorise based on merchant name keywords."""
    if tx_type == "income":
        m = merchant.lower()
        if any(w in m for w in ["salary","зарплата","оклад"]): return {"icon":"💼","name":"Зарплата"}
        return {"icon":"💰","name":"Доход"}
    m = merchant.lower()
    rules = [
        (["magnit","korzinka","makro","havas","metro","supermarket","bazar","grocery","продукт"],"🛒","Продукты"),
        (["uzum","wildberries","ozon","aliexpress","shop","store","одежд","fashion","market"],"👕","Одежда"),
        (["yandex","uber","taxi","такси","bus","metro","transport","авто","fuel","бензин","petrol","gasoline"],"🚌","Транспорт"),
        (["cafe","coffee","kafe","restaurant","ресторан","food","pizza","burger","sushi","доставк","delivery"],"🍕","Кафе"),
        (["pharmacy","apteka","farmac","health","клиник","hospital","doctor","doktor","медиц"],"💊","Здоровье"),
        (["netflix","cinema","kino","spotify","game","gaming","entertainment","развлеч"],"🎬","Развлечения"),
        (["uztel","ucell","beeline","mobiuz","internet","mobile","phone","связь","telecom"],"📱","Связь"),
        (["education","school","course","book","kitob","learn","учёба","курс"],"📚","Образование"),
        (["communal","коммунал","electric","electricity","gas","water","квартплат","жильё","aрenda"],"🏠","Жильё"),
    ]
    for keywords, icon, name in rules:
        if any(k in m for k in keywords): return {"icon":icon,"name":name}
    return {"icon":"•••","name":"Прочее"}

# ── Telegram Bot Webhook ──────────────────────────────────────────────────────
APP_URL = os.getenv("APP_URL", "https://finly-tracker.up.railway.app")

async def _register_webhook():
    """Tell Telegram to send updates to our /bot/webhook endpoint."""
    if not BOT_TOKEN:
        return
    try:
        webhook_url = f"{APP_URL}/bot/webhook"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": webhook_url, "allowed_updates": [
                    "message","callback_query",
                    "business_connection","business_message","edited_business_message"
                ]},
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                log.info(f"Webhook registered: {webhook_url}")
            else:
                log.warning(f"Webhook registration failed: {data}")
    except Exception as e:
        log.warning(f"Could not register webhook: {e}")

@app.post("/bot/webhook", include_in_schema=False)
async def telegram_webhook(request: Request, conn=Depends(db)):
    """Receive Telegram bot updates."""
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    # ── Callback query (inline button press) ──────────────────────────────────
    if "callback_query" in update:
        cq = update["callback_query"]
        user_id = cq["from"]["id"]
        data = cq.get("data","")
        msg_id = cq["message"]["message_id"]
        chat_id = cq["from"]["id"]

        parts = data.split(":")
        if len(parts) >= 3 and parts[0] == "sms":
            action = parts[1]
            pending_id = int(parts[2])

            if action == "confirm":
                try:
                    await _handle_sms_confirm(user_id, pending_id, conn=conn)
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
                            json={"chat_id":chat_id,"message_id":msg_id,"reply_markup":{"inline_keyboard":[]}},
                            timeout=5
                        )
                except Exception as e:
                    log.warning(f"sms confirm error: {e}")

            elif action == "cancel":
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM sms_pending WHERE id=%s AND user_id=%s", (pending_id, user_id))
                conn.commit()
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
                            json={"chat_id":chat_id,"message_id":msg_id,"text":"❌ Отменено."},
                            timeout=5
                        )
                except Exception: pass

            elif action == "cat":
                # Show category picker
                kb = {"inline_keyboard": [
                    [{"text":"🛒 Продукты","callback_data":f"smscat:{pending_id}:🛒:Продукты"},
                     {"text":"🍕 Кафе","callback_data":f"smscat:{pending_id}:🍕:Кафе"}],
                    [{"text":"🚌 Транспорт","callback_data":f"smscat:{pending_id}:🚌:Транспорт"},
                     {"text":"💊 Здоровье","callback_data":f"smscat:{pending_id}:💊:Здоровье"}],
                    [{"text":"👕 Одежда","callback_data":f"smscat:{pending_id}:👕:Одежда"},
                     {"text":"🎬 Развлечения","callback_data":f"smscat:{pending_id}:🎬:Развлечения"}],
                    [{"text":"📱 Связь","callback_data":f"smscat:{pending_id}:📱:Связь"},
                     {"text":"🏠 Жильё","callback_data":f"smscat:{pending_id}:🏠:Жильё"}],
                    [{"text":"📚 Образование","callback_data":f"smscat:{pending_id}:📚:Образование"},
                     {"text":"•••  Прочее","callback_data":f"smscat:{pending_id}:•••:Прочее"}],
                    [{"text":"❌ Отмена","callback_data":f"sms:cancel:{pending_id}"}],
                ]}
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
                            json={"chat_id":chat_id,"message_id":msg_id,"reply_markup":kb},
                            timeout=5
                        )
                except Exception: pass

        elif len(parts) >= 4 and parts[0] == "smscat":
            pending_id = int(parts[1])
            cat_icon = parts[2]
            cat_name = parts[3]
            try:
                await _handle_sms_confirm(user_id, pending_id, cat_icon=cat_icon, cat_name=cat_name, conn=conn)
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
                        json={"chat_id":chat_id,"message_id":msg_id,"reply_markup":{"inline_keyboard":[]}},
                        timeout=5
                    )
            except Exception as e:
                log.warning(f"smscat confirm error: {e}")

        elif len(parts) >= 2 and parts[0] == "goal":
            # Handle goal accept/reject from notification button
            action = parts[1]
            goal_id = int(parts[2])
            if action in ("accept","reject"):
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT * FROM shared_goals WHERE id=%s", (goal_id,))
                        goal = cur.fetchone()
                        if goal:
                            # Update member status
                            new_status = "accepted" if action == "accept" else "rejected"
                            cur.execute("UPDATE shared_goal_members SET status=%s WHERE goal_id=%s AND user_id=%s",
                                        (new_status, goal_id, user_id))
                            # Check if all accepted
                            cur.execute("SELECT COUNT(*) as t, SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) as a, SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) as r FROM shared_goal_members WHERE goal_id=%s", (goal_id,))
                            counts = cur.fetchone()
                            if counts["r"] > 0:
                                cur.execute("UPDATE shared_goals SET status='rejected' WHERE id=%s", (goal_id,))
                            elif counts["a"] == counts["t"]:
                                cur.execute("UPDATE shared_goals SET status='active' WHERE id=%s", (goal_id,))
                            else:
                                # Legacy single partner
                                if goal["partner_id"] == user_id:
                                    cur.execute("UPDATE shared_goals SET status=%s WHERE id=%s",
                                                ("active" if action=="accept" else "rejected", goal_id))
                    conn.commit()
                    reply_text = "✅ Цель принята! Открой Finly чтобы посмотреть." if action=="accept" else "❌ Цель отклонена."
                    await send_tg_message(user_id, reply_text)
                    if action == "accept" and goal:
                        await send_tg_message(goal["creator_id"], f"✅ Участник принял совместную цель <b>{goal['emoji']} {goal['name']}</b>!")
                except Exception as e:
                    log.warning(f"Goal callback error: {e}")

        # Answer callback to remove loading state
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": cq["id"]},
                    timeout=5
                )
        except Exception: pass
        return {"ok": True}

    # ── Business connection (user connects/disconnects bot as Secretary Bot) ─────
    if "business_connection" in update:
        bc = update["business_connection"]
        conn_id = bc["id"]
        user_tg_id = bc["user"]["id"]
        is_enabled = bc.get("is_enabled", True)
        can_reply  = bc.get("can_reply", False)
        log.info(f"BUSINESS_CONN id={conn_id!r} user={user_tg_id} enabled={is_enabled} can_reply={can_reply}")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO business_connections (connection_id, user_id, can_reply, is_enabled)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (connection_id) DO UPDATE
                    SET is_enabled=EXCLUDED.is_enabled, can_reply=EXCLUDED.can_reply
            """, (conn_id, user_tg_id, can_reply, is_enabled))
        conn.commit()
        if is_enabled:
            kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
            await send_tg_message(user_tg_id,
                "✅ <b>Finly подключён как секретарь!</b>\n\n"
                "Банковские SMS из выбранных чатов теперь записываются автоматически — "
                "никаких пересылок не нужно.\n\n"
                "💡 Убедись, что в настройках Telegram ты выбрал чат с банк-ботом "
                "(например, HUMOcardbot).", kb)
        return {"ok": True}

    # ── Business message (message from a chat the user connected) ─────────────
    if "business_message" in update:
        bmsg = update["business_message"]
        bc_id = bmsg.get("business_connection_id", "")
        btext = bmsg.get("text", "").strip()
        # Privacy: don't log the content of users' personal chat messages — only
        # whether it looked like a bank notification.
        log.info(f"BUSINESS_MSG conn={bc_id!r} len={len(btext)} bank={_parse_bank_sms(btext) is not None}")
        if bc_id and btext:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id FROM business_connections
                    WHERE connection_id=%s AND is_enabled=TRUE LIMIT 1
                """, (bc_id,))
                row = cur.fetchone()
            if not row:
                log.warning(f"BUSINESS_MSG no stored connection for {bc_id!r} — user must reconnect secretary")
            if row:
                buid = row["user_id"]
                parsed = _parse_bank_sms(btext)
                if parsed:
                    p = parsed
                    today_str = date.today().isoformat()
                    now_str = datetime.now().strftime("%H:%M")
                    auto_cat = _guess_category(p["merchant"], p["tx_type"])

                    # Look up registered card by last4
                    card_alias = None
                    if p["card_last4"]:
                        with conn.cursor() as cur:
                            cur.execute("""
                                SELECT alias, auto_log FROM bank_cards
                                WHERE user_id=%s AND card_last4=%s LIMIT 1
                            """, (buid, p["card_last4"]))
                            card_row = cur.fetchone()
                        if card_row:
                            if not card_row["auto_log"]:
                                return {"ok": True}  # user disabled auto-log for this card
                            card_alias = card_row["alias"] or f"*{p['card_last4']}"
                        else:
                            # Card not registered — notify user and skip
                            kb = {"inline_keyboard": [[{"text":"💳 Добавить карту",
                                "web_app":{"url":APP_URL+"?page=cards"}}]]}
                            await send_tg_message(buid,
                                f"⚠️ Карта <b>*{p['card_last4']}</b> не добавлена в Finly.\n\n"
                                f"Добавь карту в разделе Карты, чтобы транзакции записывались автоматически.", kb)
                            return {"ok": True}

                    account_name = card_alias or "Карта"
                    try:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO transactions
                                    (user_id,type,cat_icon,cat_name,note,amount,
                                     date_str,time_str,currency,account,source)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS',%s,'business_bot')
                            """, (buid, p["tx_type"], auto_cat["icon"], auto_cat["name"],
                                  p["merchant"] or p["bank"], p["amount"],
                                  today_str, now_str, account_name))
                            cur.execute("UPDATE users SET xp=xp+5 WHERE id=%s", (buid,))
                        conn.commit()
                        sign = "−" if p["tx_type"] == "expense" else "+"
                        bal_str = f"\n💰 {p['balance']:,} сум".replace(",","ㅤ") if p["balance"] else ""
                        kb = {"inline_keyboard": [[{
                            "text":"📱 Открыть Finly",
                            "web_app":{"url":APP_URL+"?page=add"}
                        }]]}
                        await send_tg_message(buid,
                            f"🔄 <b>Авто-запись</b>\n\n"
                            f"{auto_cat['icon']} {auto_cat['name']}\n"
                            f"<b>{sign}{p['amount']:,} сум</b>{bal_str}\n"
                            f"💳 {account_name}".replace(",","ㅤ"), kb)
                    except Exception as e:
                        log.warning(f"Business bot auto-save error: {e}")
        return {"ok": True}

    # ── Incoming message ───────────────────────────────────────────────────────
    if "message" not in update:
        return {"ok": True}

    msg = update["message"]
    chat = msg.get("chat", {})
    chat_type = chat.get("type", "private")
    chat_id = chat.get("id")
    sender = msg.get("from", {})
    user_id = sender.get("id")
    text = msg.get("text","").strip()

    # ── Group message handling ─────────────────────────────────────────────────
    if chat_type in ("group", "supergroup"):
        # /link command: bind this group to the commanding user
        if text.startswith("/link") and user_id:
            title = chat.get("title", "")
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO linked_groups (chat_id, user_id, chat_title, active)
                    VALUES (%s, %s, %s, TRUE)
                    ON CONFLICT (chat_id) DO UPDATE SET user_id=EXCLUDED.user_id,
                        chat_title=EXCLUDED.chat_title, active=TRUE
                """, (chat_id, user_id, title))
            conn.commit()
            kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
            await send_tg_message(chat_id,
                "✅ Группа привязана к твоему аккаунту!\n\n"
                "Теперь любое SMS от банка в этой группе будет автоматически "
                "записываться в Finly без подтверждения.", kb)
            return {"ok": True}

        if text.startswith("/unlink") and user_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE linked_groups SET active=FALSE WHERE chat_id=%s AND user_id=%s",
                            (chat_id, user_id))
            conn.commit()
            await send_tg_message(chat_id, "❎ Группа отвязана от Finly.")
            return {"ok": True}

        # Auto-process bank SMS posted in linked group
        if text:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM linked_groups WHERE chat_id=%s AND active=TRUE LIMIT 1",
                            (chat_id,))
                row = cur.fetchone()
            if row:
                linked_uid = row["user_id"]
                parsed = _parse_bank_sms(text)
                if parsed:
                    p = parsed
                    today_str = date.today().isoformat()
                    now_str = datetime.now().strftime("%H:%M")
                    auto_cat = _guess_category(p["merchant"], p["tx_type"])
                    try:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO transactions
                                    (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,
                                     currency,account,source)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS','Карта','sms_group')
                            """, (linked_uid, p["tx_type"], auto_cat["icon"], auto_cat["name"],
                                  p["merchant"] or p["bank"], p["amount"], today_str, now_str))
                            cur.execute("UPDATE users SET xp=xp+5 WHERE id=%s", (linked_uid,))
                        conn.commit()
                        sign = "−" if p["tx_type"] == "expense" else "+"
                        bal_str = f"\n💰 {p['balance']:,} сум".replace(",","ㅤ") if p["balance"] else ""
                        kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL+"?page=add"}}]]}
                        await send_tg_message(linked_uid,
                            f"🔄 <b>Авто-запись из группы</b>\n\n"
                            f"{auto_cat['icon']} {auto_cat['name']}\n"
                            f"<b>{sign}{p['amount']:,} сум</b>{bal_str}\n"
                            f"🏦 {p['bank'] or 'Банк'}".replace(",","ㅤ"), kb)
                    except Exception as e:
                        log.warning(f"Group SMS auto-save error: {e}")
        return {"ok": True}

    # ── Private chat only below ────────────────────────────────────────────────
    if not user_id:
        return {"ok": True}

    # ── Voice message handling ─────────────────────────────────────────────────
    if "voice" in msg:
        if not _stt_client:
            await send_tg_message(user_id,
                "🎤 Голосовой ввод пока недоступен — требуется настройка GROQ_API_KEY или OPENAI_API_KEY.")
            return {"ok": True}
        # Download the OGG file
        try:
            file_id = msg["voice"]["file_id"]
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                    params={"file_id": file_id}, timeout=10)
                file_path = r.json()["result"]["file_path"]
                r2 = await client.get(
                    f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=30)
                ogg_bytes = r2.content
        except Exception as e:
            log.warning(f"Voice download error: {e}")
            await send_tg_message(user_id, "❌ Не удалось скачать голосовое. Попробуй ещё раз.")
            return {"ok": True}

        # Transcribe
        transcript = await transcribe_ogg_bytes(ogg_bytes)
        if not transcript:
            await send_tg_message(user_id, "❌ Не удалось распознать речь. Попробуй говорить чётче или напиши текстом.")
            return {"ok": True}

        amount   = _parse_voice_amount(transcript)
        cat_icon, cat_name = _parse_voice_category(transcript)
        tx_type  = _parse_voice_type(transcript)

        if not amount:
            await send_tg_message(user_id,
                f"🎤 Распознано: <i>{transcript}</i>\n\n"
                "❓ Не удалось определить сумму. Попробуй сказать: <b>«потратил 50 тысяч на еду»</b>")
            return {"ok": True}

        # Save transaction directly
        today_str = date.today().isoformat()
        now_str   = datetime.now().strftime("%H:%M")
        sign      = "+" if tx_type == "income" else "−"
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,account,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS','Наличка','voice') RETURNING id
                """, (user_id, tx_type, cat_icon, cat_name,
                      transcript[:80], amount, today_str, now_str))
                cur.execute("SELECT streak,xp,level FROM users WHERE id=%s", (user_id,))
                u = cur.fetchone() or {}
                xp_gain = 10
                cur.execute("UPDATE users SET xp=xp+%s WHERE id=%s", (xp_gain, user_id))
                # streak
                cur.execute("SELECT last_log_date FROM users WHERE id=%s", (user_id,))
                lld = (cur.fetchone() or {}).get("last_log_date")
                if lld == date.today():
                    pass
                elif lld and (date.today() - lld).days == 1:
                    cur.execute("UPDATE users SET streak=streak+1,streak_record=GREATEST(streak_record,streak+1),last_log_date=%s WHERE id=%s", (today_str, user_id))
                else:
                    cur.execute("UPDATE users SET streak=1,streak_record=GREATEST(streak_record,1),last_log_date=%s WHERE id=%s", (today_str, user_id))
            conn.commit()
        except Exception as e:
            log.warning(f"Voice tx save error: {e}")
            await send_tg_message(user_id, "❌ Ошибка сохранения. Попробуй ещё раз.")
            return {"ok": True}

        kb = {"inline_keyboard": [[
            {"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}},
        ]]}
        await send_tg_message(user_id,
            f"✅ <b>Записано по голосу!</b>\n\n"
            f"{cat_icon} <b>{cat_name}</b>\n"
            f"<b>{sign}{amount:,} сум</b>\n"
            f"🎤 <i>{transcript}</i>\n\n"
            f"⭐ +{xp_gain} XP".replace(",","ㅤ"),
            kb)
        return {"ok": True}

    # Detect forwarded message — extract text even if forwarded from a bot
    is_forward = "forward_origin" in msg or "forward_from" in msg or "forward_from_chat" in msg
    if is_forward and not text:
        # Some forwards carry caption instead of text
        text = msg.get("caption", "").strip()
    if not text:
        return {"ok": True}

    # /start command
    if text.startswith("/start"):
        # Deep link from "Manage Bot" button in a business-connected chat
        if "bizChat" in text:
            kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
            await send_tg_message(user_id,
                "⚙️ <b>Управление секретарём</b>\n\n"
                "Finly автоматически читает выбранные чаты и записывает банковские транзакции.\n\n"
                "Чтобы изменить настройки: Telegram → Настройки → Telegram Business → Чат-боты", kb)
            return {"ok": True}
        kb = {"inline_keyboard": [
            [{"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}}],
        ]}
        await send_tg_message(user_id,
            "👋 Привет! Я <b>Finly Bot</b>.\n\n"
            "📲 <b>Способы записи:</b>\n"
            "• Перешли SMS от банка сюда — я сразу запишу\n"
            "• 🎤 Голосовое: <i>«потратил 50к на кафе»</i>\n"
            "• Просто напиши: <i>«20 000 продукты»</i>\n\n"
            "🤖 <b>Авто-синхронизация (Humo bot / Uzcard):</b>\n"
            "1. Создай группу в Telegram\n"
            "2. Добавь туда <b>Humo bot</b> (или другой банк-бот) + меня\n"
            "3. Напиши в группе <code>/link</code>\n"
            "4. Готово — все SMS записываются автоматически!\n\n"
            "🏦 Банки: Uzcard, HUMO, Kapitalbank, Hamkorbank, TBC, Alif",
            kb)
        return {"ok": True}

    # Try to parse as bank SMS
    parsed = _parse_bank_sms(text)
    if not parsed and is_forward:
        log.info(f"HUMO_DEBUG forwarded unparsed: {repr(text[:300])}")
    if not parsed:
        # Try as voice-style text transaction
        amount   = _parse_voice_amount(text)
        cat_icon, cat_name = _parse_voice_category(text)
        tx_type  = _parse_voice_type(text)
        if amount and amount >= 100:
            today_str = date.today().isoformat()
            now_str   = datetime.now().strftime("%H:%M")
            sign      = "+" if tx_type == "income" else "−"
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO transactions (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,account)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS','Наличка') RETURNING id
                    """, (user_id, tx_type, cat_icon, cat_name, text[:80], amount, today_str, now_str))
                    cur.execute("UPDATE users SET xp=xp+10 WHERE id=%s", (user_id,))
                    cur.execute("SELECT last_log_date FROM users WHERE id=%s", (user_id,))
                    lld = (cur.fetchone() or {}).get("last_log_date")
                    if lld != date.today():
                        if lld and (date.today() - lld).days == 1:
                            cur.execute("UPDATE users SET streak=streak+1,streak_record=GREATEST(streak_record,streak+1),last_log_date=%s WHERE id=%s", (today_str, user_id))
                        else:
                            cur.execute("UPDATE users SET streak=1,streak_record=GREATEST(streak_record,1),last_log_date=%s WHERE id=%s", (today_str, user_id))
                conn.commit()
                kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
                await send_tg_message(user_id,
                    f"✅ <b>Записано!</b>\n{cat_icon} {cat_name}\n<b>{sign}{amount:,} сум</b>\n⭐ +10 XP".replace(",","ㅤ"), kb)
            except Exception as e:
                log.warning(f"Text tx save error: {e}")
                kb = {"inline_keyboard": [[{"text":"📱 Открыть Finly","web_app":{"url":APP_URL}}]]}
                await send_tg_message(user_id, "❌ Ошибка сохранения.", kb)
            return {"ok": True}

        # Not parseable — give helpful response
        kb = {"inline_keyboard": [[
            {"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}},
        ]]}
        await send_tg_message(user_id,
            "💬 Перешли мне <b>SMS от банка</b> или напиши/скажи голосом:\n"
            "<i>«50 тысяч кафе»</i> · <i>«потратил 200к продукты»</i>\n\n"
            "🤖 Для авто-синхронизации с Humo bot:\n"
            "Создай группу → добавь банк-бот + меня → напиши <code>/link</code>\n\n"
            "🏦 Банки: Uzcard, HUMO, Kapitalbank, Hamkorbank, TBC, Alif",
            kb)
        return {"ok": True}

    # Save pending + send confirmation
    p = parsed

    # Look up card alias for display (card match happens at confirm-time)
    card_alias = None
    if p["card_last4"]:
        with conn.cursor() as cur:
            cur.execute("SELECT alias FROM bank_cards WHERE user_id=%s AND card_last4=%s LIMIT 1",
                        (user_id, p["card_last4"]))
            crd = cur.fetchone()
        if crd:
            card_alias = crd["alias"] or f"*{p['card_last4']}"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sms_pending (user_id,raw_text,bank,card_last4,amount,tx_type,merchant,balance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (user_id, text[:500], p["bank"], p["card_last4"], p["amount"],
              p["tx_type"], p["merchant"] or "", p["balance"]))
        pending_id = cur.fetchone()["id"]
    conn.commit()

    sign = "−" if p["tx_type"]=="expense" else "+"
    emoji = "💳" if p["tx_type"]=="expense" else "💰"
    bank_str = card_alias or (f"{p['bank']} *{p['card_last4']}" if p["card_last4"] else p["bank"] or "Банк")
    merchant_str = p["merchant"] or ("Покупка" if p["tx_type"]=="expense" else "Пополнение")
    bal_str = f"\n💰 Остаток: {p['balance']:,} сум".replace(",","ㅤ") if p["balance"] else ""
    auto_cat = _guess_category(p["merchant"], p["tx_type"])

    msg_text = (
        f"{emoji} <b>{sign}{p['amount']:,} сум</b>\n"
        f"🏪 {merchant_str}\n"
        f"🏦 {bank_str}"
        f"{bal_str}\n\n"
        f"Категория: {auto_cat['icon']} {auto_cat['name']}\n"
        f"Записать как {'расход' if p['tx_type']=='expense' else 'доход'}?"
    ).replace(",","ㅤ")

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Записать", "callback_data": f"sms:confirm:{pending_id}"},
        {"text": "🏷 Категория", "callback_data": f"sms:cat:{pending_id}"},
        {"text": "❌ Нет", "callback_data": f"sms:cancel:{pending_id}"},
    ]]}
    await send_tg_message(user_id, msg_text, keyboard)
    return {"ok": True}

async def _handle_sms_confirm(user_id: int, pending_id: int,
                               cat_icon: str = None, cat_name: str = None, conn=None):
    """Commit a pending SMS transaction to the DB."""
    close_conn = (conn is None)
    if conn is None:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sms_pending WHERE id=%s AND user_id=%s", (pending_id, user_id))
            pending = cur.fetchone()
            if not pending:
                await send_tg_message(user_id, "❌ Запись не найдена или уже обработана.")
                return

            # ── Look up registered card by last 4 digits ────────────────────
            card_id = None
            card_alias = None
            if pending["card_last4"]:
                cur.execute("""
                    SELECT id, alias FROM bank_cards
                    WHERE user_id=%s AND card_last4=%s LIMIT 1
                """, (user_id, pending["card_last4"]))
                row = cur.fetchone()
                if row:
                    card_id   = row["id"]
                    card_alias = row["alias"] or f"Карта *{pending['card_last4']}"

            # Card not registered → still log it (to a generic account), then
            # offer to add the card for cleaner tracking.
            card_unregistered = bool(pending["card_last4"]) and not card_id
            if card_unregistered:
                account = f"Карта *{pending['card_last4']}"
            else:
                account = card_alias or pending["bank"] or "Карта"
            auto = _guess_category(pending["merchant"], pending["tx_type"])
            icon = cat_icon or auto["icon"]
            name = cat_name or auto["name"]
            merchant = pending["merchant"] or ("Покупка" if pending["tx_type"]=="expense" else "Пополнение")

            today_str = str(date.today())
            time_str  = datetime.now().strftime("%H:%M")
            cur.execute("""
                INSERT INTO transactions
                (user_id,type,cat_icon,cat_name,note,amount,date_str,time_str,currency,recurring,account,source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'UZS',FALSE,%s,'forward')
            """, (user_id, pending["tx_type"], icon, name, merchant,
                  pending["amount"], today_str, time_str, account))

            # Update card balance
            if card_id and pending["balance"]:
                cur.execute("UPDATE bank_cards SET balance=%s, updated_at=NOW() WHERE id=%s",
                            (pending["balance"], card_id))

            cur.execute("DELETE FROM sms_pending WHERE id=%s", (pending_id,))
            cur.execute("UPDATE users SET xp=xp+10 WHERE id=%s", (user_id,))
        conn.commit()

        sign = "−" if pending["tx_type"]=="expense" else "+"
        done_text = f"✅ Записано!\n{icon} {name} · {sign}{pending['amount']:,} сум\n🏦 {account}".replace(",","ㅤ")
        if card_unregistered:
            done_text += (
                f"\n\n💳 Карта <b>*{pending['card_last4']}</b> ещё не добавлена. "
                f"Добавь её, чтобы транзакции группировались по карте."
            )
            kb = {"inline_keyboard": [[
                {"text": f"💳 Добавить карту *{pending['card_last4']}",
                 "web_app": {"url": APP_URL + f"?page=cards&card={pending['card_last4']}"}},
            ]]}
        else:
            kb = {"inline_keyboard": [[
                {"text": "📱 Открыть Finly", "web_app": {"url": APP_URL}},
            ]]}
        await send_tg_message(user_id, done_text, kb)
    finally:
        if close_conn:
            conn.close()

# ── Admin HTML (single definition, no-cache) ─────────────────────────────────
@app.get("/admin", include_in_schema=False)
def serve_admin():
    return FileResponse(
        os.path.join(_HERE, "admin.html"),
        media_type="text/html",
        headers=_NO_CACHE,
    )

# ── SPA catch-all (MUST be last — after all /api/* routes) ──────────────────
@app.get("/{full_path:path}", include_in_schema=False)
def catch_all(full_path: str):
    """Fallback: serve the React SPA for any non-API, non-static path."""
    return FileResponse(os.path.join(_HERE, "index.html"), media_type="text/html", headers=_NO_CACHE)
