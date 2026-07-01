import os, logging, threading, time, hmac, hashlib, json, urllib.parse
import datetime
import re
import tempfile
import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton,
)
import psycopg2
import psycopg2.extras
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
API_PORT     = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
API_BASE     = f"http://127.0.0.1:{API_PORT}"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

# STT client — Groq preferred (fast + free via openai-compat endpoint), OpenAI fallback
# Both use the already-installed openai package — no extra package needed for Groq
_stt_client = None
_stt_provider = None   # "groq" | "openai"
try:
    from openai import OpenAI
    if GROQ_API_KEY:
        _stt_client   = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
        _stt_provider = "groq"
        logging.getLogger("finly.bot").info("Groq STT ready via openai-compat (whisper-large-v3-turbo)")
    elif OPENAI_API_KEY:
        _stt_client   = OpenAI(api_key=OPENAI_API_KEY)
        _stt_provider = "openai"
        logging.getLogger("finly.bot").info("OpenAI Whisper STT ready")
    else:
        logging.getLogger("finly.bot").warning("No GROQ_API_KEY or OPENAI_API_KEY — voice disabled")
except ImportError:
    logging.getLogger("finly.bot").warning("openai package not installed — voice disabled")
_openai_client = _stt_client  # legacy compat

# Uzbekistan Standard Time (UTC+5, no DST)
UZT = pytz.timezone("Asia/Tashkent")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("finly.bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Per-user conversation state: {uid: {"step": str, "data": dict}}
_state: dict = {}
# Tracks (uid, date_str, time_str) already notified this session to avoid duplicates
_notif_sent: set = set()

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return "0"

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_user(uid):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            return cur.fetchone()
    except Exception as e:
        log.error(f"DB error: {e}")
        return None
    finally:
        try: conn.close()
        except: pass

def get_all_users_for_notify():
    """Return all users with their notification/report prefs."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id, first_name, notif_pref_json, report_pref_json, streak, last_log_date FROM users")
            return cur.fetchall()
    except Exception as e:
        log.error(f"DB error in get_all_users_for_notify: {e}")
        return []
    finally:
        try: conn.close()
        except: pass

def make_init_data(tg_user, start_param: str = "") -> str:
    """Build a valid X-Init-Data header value so the bot can call the API as the user."""
    user_obj = {
        "id": tg_user.id,
        "first_name": tg_user.first_name or "",
        "username": tg_user.username or "",
    }
    user_json  = json.dumps(user_obj, ensure_ascii=False, separators=(",", ":"))
    auth_date  = str(int(time.time()))
    params     = {"auth_date": auth_date, "user": user_json}
    if start_param:
        params["start_param"] = start_param   # included in HMAC + passed to API
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret     = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    hash_val   = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    parts = [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items()]
    parts.append(f"hash={hash_val}")
    return "&".join(parts)

def make_init_data_from_id(uid: int, first_name: str = "", username: str = "") -> str:
    """Build init data from raw user ID (for scheduler, no telebot user object)."""
    class _FakeUser:
        def __init__(self, uid, fn, un):
            self.id = uid; self.first_name = fn; self.username = un
    return make_init_data(_FakeUser(uid, first_name, username))

def api(method: str, path: str, tg_user, body=None):
    """Call the local FastAPI. Returns parsed JSON or None on error."""
    try:
        headers = {"X-Init-Data": make_init_data(tg_user)}
        url = API_BASE + path
        if method == "GET":
            res = requests.get(url, headers=headers, timeout=12)
        elif method == "POST":
            headers["Content-Type"] = "application/json"
            res = requests.post(url, json=body, headers=headers, timeout=12)
        elif method == "PATCH":
            headers["Content-Type"] = "application/json"
            res = requests.patch(url, json=body, headers=headers, timeout=12)
        else:
            return None
        if not res.ok:
            log.error(f"API {method} {path} → {res.status_code}: {res.text[:300]}")
            return None
        return res.json()
    except Exception as e:
        log.error(f"API {method} {path} exception: {e}")
        return None

def api_with_id(method: str, path: str, uid: int, first_name: str = "", body=None):
    """Call the API using a raw user ID (no telebot user object)."""
    try:
        headers = {"X-Init-Data": make_init_data_from_id(uid, first_name)}
        url = API_BASE + path
        if method == "GET":
            res = requests.get(url, headers=headers, timeout=12)
        elif method == "POST":
            headers["Content-Type"] = "application/json"
            res = requests.post(url, json=body, headers=headers, timeout=12)
        elif method == "PATCH":
            headers["Content-Type"] = "application/json"
            res = requests.patch(url, json=body, headers=headers, timeout=12)
        else:
            return None
        res.raise_for_status()
        return res.json()
    except Exception as e:
        log.error(f"API {method} {path} (uid={uid}): {e}")
        return None

# ── Keyboards ─────────────────────────────────────────────────────────────────

def shortcut_kb():
    """Persistent reply keyboard shown below the text input."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row(KeyboardButton("➕ Доход"), KeyboardButton("➖ Расход"))
    kb.row(KeyboardButton("📊 Отчёт"), KeyboardButton("💰 Баланс"))
    return kb

def main_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.row(
        InlineKeyboardButton("➕ Добавить запись", callback_data="do:add"),
        InlineKeyboardButton("📊 Баланс",          callback_data="do:balance"),
    )
    kb.row(
        InlineKeyboardButton("🧾 Последние",       callback_data="do:recent"),
        InlineKeyboardButton("🎯 Бюджеты",         callback_data="do:budgets"),
    )
    kb.add(InlineKeyboardButton("🔥 Статистика",   callback_data="do:stats"))
    return kb

def add_type_kb():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💸 Расход",  callback_data="add:type:expense"),
        InlineKeyboardButton("💵 Доход",   callback_data="add:type:income"),
    )
    kb.add(InlineKeyboardButton("❌ Отмена", callback_data="add:cancel"))
    return kb

def cat_kb(cats):
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = [
        InlineKeyboardButton(
            f"{c['icon']} {c['name']}",
            callback_data=f"add:cat:{c['icon']}|{c['name']}"
        )
        for c in cats[:15]
    ]
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("❌ Отмена", callback_data="add:cancel"))
    return kb

def post_save_kb():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Ещё",     callback_data="do:add"),
        InlineKeyboardButton("📊 Баланс", callback_data="do:balance"),
    )
    kb.add(InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL)))
    return kb

# ── Command handlers ──────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid  = message.from_user.id
    name = message.from_user.first_name or "друг"

    # Extract optional start parameter (e.g. ref_XXXX from referral link)
    msg_text    = message.text or ""
    start_param = msg_text.split(" ", 1)[1].strip() if " " in msg_text else ""

    # Check if user already exists BEFORE calling auth (to detect new users)
    existing = get_user(uid)
    is_brand_new = (existing is None)

    # Call /api/auth with start_param so the server handles referral attribution,
    # user creation, XP etc. — this is the authoritative registration path.
    try:
        headers = {"X-Init-Data": make_init_data(message.from_user, start_param=start_param)}
        import requests as _req
        _req.post(f"{API_BASE}/api/auth", json={}, headers=headers, timeout=12)
    except Exception as e:
        log.warning(f"cmd_start auth call failed: {e}")

    # Reload user after potential creation
    user = get_user(uid)

    if is_brand_new:
        text = (
            f"🎉 Привет, *{name}*! Добро пожаловать в *Finly*.\n\n"
            f"Finly — твой умный финансовый трекер:\n"
            f"💸 Записывай расходы и доходы мгновенно\n"
            f"📊 Смотри отчёты и аналитику\n"
            f"🎯 Ставь цели и копилки\n"
            f"🔥 Зарабатывай XP и строй серии\n\n"
            f"Открой приложение, чтобы начать 👇"
        )
        # Notify referrer that a new user joined via their link
        if start_param.startswith("ref_") and user:
            try:
                ref_uid = _find_referrer_uid(start_param[4:])
                if ref_uid and ref_uid != uid:
                    bot.send_message(ref_uid,
                        f"🎉 *{name}* присоединился к Finly по твоей ссылке!\n"
                        f"⭐ +100 XP — спасибо за приглашение!",
                        parse_mode="Markdown")
            except Exception:
                pass
    else:
        text = (
            f"👋 С возвращением, *{name}*!\n\n"
            f"🔥 Серия: *{user['streak']}* дн.  ·  ⭐ *{user['xp']}* XP  ·  🏅 Ур. *{user['level']}*\n\n"
            f"Что делаем?"
        )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=shortcut_kb())
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=main_kb())


def _find_referrer_uid(ref_part: str) -> int | None:
    """Find the referrer's user_id from a ref_code or legacy numeric uid."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Try numeric user_id (legacy format)
            try:
                maybe_id = int(ref_part)
                cur.execute("SELECT id FROM users WHERE id=%s", (maybe_id,))
                row = cur.fetchone()
                if row:
                    return row["id"]
            except ValueError:
                pass
            # Try ref_code lookup
            cur.execute("SELECT id FROM users WHERE ref_code=%s", (ref_part,))
            row = cur.fetchone()
            return row["id"] if row else None
    except Exception:
        return None
    finally:
        try: conn.close()
        except: pass

# ── Shortcut keyboard button handlers ────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "➕ Доход")
def btn_income(message):
    _state[message.from_user.id] = {"step": "amount", "data": {"type": "income"}}
    bot.send_message(message.chat.id, "💵 *Доход*\n\nВведите сумму (в сумах):")

@bot.message_handler(func=lambda m: m.text == "➖ Расход")
def btn_expense(message):
    _state[message.from_user.id] = {"step": "amount", "data": {"type": "expense"}}
    bot.send_message(message.chat.id, "💸 *Расход*\n\nВведите сумму (в сумах):")

@bot.message_handler(func=lambda m: m.text == "📊 Отчёт")
def btn_report(message):
    _show_recent(message.chat.id, message.from_user)

@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def btn_balance_kb(message):
    _show_balance(message.chat.id, message.from_user)

@bot.message_handler(commands=["add", "новая", "добавить"])
def cmd_add(message):
    _begin_add(message.chat.id, message.from_user.id)

@bot.message_handler(commands=["balance", "баланс"])
def cmd_balance(message):
    _show_balance(message.chat.id, message.from_user)

@bot.message_handler(commands=["recent", "последние"])
def cmd_recent(message):
    _show_recent(message.chat.id, message.from_user)

@bot.message_handler(commands=["budgets", "бюджеты"])
def cmd_budgets(message):
    _show_budgets(message.chat.id, message.from_user)

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    _show_stats(message.chat.id, message.from_user.id)

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id,
        "📖 *Finly — команды*\n\n"
        "/start — главное меню\n"
        "/add — добавить запись\n"
        "/balance — текущий баланс\n"
        "/recent — последние 5 операций\n"
        "/budgets — бюджеты месяца\n"
        "/stats — серия и XP\n"
        "/link — войти в приложение для iPhone\n\n"
        "Или откройте мини-приложение для полного функционала 👆"
    )

@bot.message_handler(commands=["link"])
def cmd_link(message):
    """Generate a 6-digit code to log into the native iOS app."""
    import random as _random
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        bot.send_message(message.chat.id, "Сначала откройте приложение через /start 👆")
        return
    code = f"{_random.randint(0, 999999):06d}"
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # one active code per user — clear old ones
            cur.execute("DELETE FROM link_codes WHERE user_id=%s", (uid,))
            cur.execute(
                "INSERT INTO link_codes (code, user_id, expires_at) VALUES (%s,%s, NOW() + INTERVAL '5 minutes')",
                (code, uid))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"link code error: {e}")
        bot.send_message(message.chat.id, "Ошибка. Попробуйте ещё раз.")
        return
    pretty = " ".join(code)
    bot.send_message(message.chat.id,
        f"🔗 *Вход в приложение Finly для iPhone*\n\n"
        f"Ваш код:  `{pretty}`\n\n"
        f"Введите его в приложении в течение *5 минут*.\n"
        f"Код одноразовый — никому не передавайте.",
        parse_mode="Markdown")

# ── Inline-button dispatcher ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid  = call.from_user.id
    data = call.data

    # ── Voice confirm ──
    if data.startswith("vc:"):
        parts  = data.split(":")
        action = parts[1]          # yes / edit / cancel
        vc_uid = int(parts[2]) if len(parts) > 2 else uid
        # NOTE: do NOT pre-answer here — each handler answers exactly once inside
        if action == "yes":
            _handle_voice_confirm(call, vc_uid)
        elif action == "edit":
            _handle_voice_edit(call, vc_uid)
        elif action == "cancel":
            _handle_voice_cancel(call, vc_uid)
        else:
            _safe_answer(call.id)
        return

    # ── Friend request accept/reject ──
    if data.startswith("friend:accept:") or data.startswith("friend:reject:"):
        parts      = data.split(":")
        action     = "accept" if parts[1] == "accept" else "reject"
        req_uid    = int(parts[2])   # the requester's user_id
        result     = api("PATCH", f"/api/friends/{req_uid}", call.from_user, body={"action": action})
        if result and result.get("ok"):
            label = "✅ Запрос принят!" if action == "accept" else "❌ Запрос отклонён."
            bot.answer_callback_query(call.id, label)
            try:
                suffix = "\n\n✅ Вы приняли запрос в друзья!" if action == "accept" else "\n\n❌ Вы отклонили запрос."
                bot.edit_message_text(call.message.text + suffix, call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Не удалось обработать. Попробуйте в приложении.")
        return

    # ── Debt approve/reject ──
    if data.startswith("debt:approve:") or data.startswith("debt:reject:"):
        parts  = data.split(":")
        action = parts[1]   # "approve" or "reject"
        debt_id = int(parts[2])
        result = api("PATCH", f"/api/social_debts/{debt_id}", call.from_user, body={"action": action})
        if result and result.get("ok"):
            label = "✅ Долг подтверждён!" if action == "approve" else "❌ Долг отклонён."
            bot.answer_callback_query(call.id, label)
            try:
                new_text = call.message.text + f"\n\n{'✅ Вы подтвердили этот долг.' if action=='approve' else '❌ Вы отклонили этот долг.'}"
                bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Ошибка. Попробуйте через приложение.")
        return

    # ── Shared goal accept/reject ──
    if data.startswith("goal:accept:") or data.startswith("goal:reject:"):
        parts   = data.split(":")
        action  = "accept" if parts[1] == "accept" else "reject"
        goal_id = int(parts[2])
        result  = api("PATCH", f"/api/shared_goals/{goal_id}", call.from_user, body={"action": action})
        if result and result.get("ok"):
            label = "✅ Цель принята!" if action == "accept" else "❌ Приглашение отклонено."
            bot.answer_callback_query(call.id, label)
            try:
                suffix = "\n\n✅ Вы приняли совместную цель!" if action == "accept" else "\n\n❌ Вы отклонили приглашение."
                bot.edit_message_text(call.message.text + suffix, call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "❌ Ошибка. Попробуйте через приложение.")
        return

    # ── Main menu shortcuts ──
    if data == "do:add":
        bot.answer_callback_query(call.id)
        _begin_add(call.message.chat.id, uid)
        return
    if data == "do:balance":
        bot.answer_callback_query(call.id)
        _show_balance(call.message.chat.id, call.from_user)
        return
    if data == "do:recent":
        bot.answer_callback_query(call.id)
        _show_recent(call.message.chat.id, call.from_user)
        return
    if data == "do:budgets":
        bot.answer_callback_query(call.id)
        _show_budgets(call.message.chat.id, call.from_user)
        return
    if data == "do:stats":
        bot.answer_callback_query(call.id)
        _show_stats(call.message.chat.id, uid)
        return

    # ── Add flow ──
    if data == "add:cancel":
        _state.pop(uid, None)
        bot.answer_callback_query(call.id, "Отменено")
        try:
            bot.edit_message_text("❌ Отменено.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return

    if data.startswith("add:type:"):
        tx_type = data.split(":")[2]
        _state[uid] = {"step": "amount", "data": {"type": tx_type}}
        bot.answer_callback_query(call.id)
        label = "Расход" if tx_type == "expense" else "Доход"
        try:
            bot.edit_message_text(
                f"➕ *{label}*\n\nВведите сумму (в сумах):",
                call.message.chat.id, call.message.message_id
            )
        except Exception:
            pass
        return

    if data.startswith("add:cat:"):
        raw = data[len("add:cat:"):]
        icon, _, name = raw.partition("|")
        st = _state.get(uid)
        if not st:
            bot.answer_callback_query(call.id, "Сессия истекла. /add")
            return
        st["data"].update({"cat_icon": icon, "cat_name": name})
        bot.answer_callback_query(call.id)
        _save_tx(call, st)
        return

    # ── SMS bank card callbacks (sms:confirm/cancel/cat and smscat:) ──────────
    if data.startswith("sms:") or data.startswith("smscat:"):
        try:
            update_payload = {
                "callback_query": {
                    "id": call.id,
                    "from": {
                        "id": call.from_user.id,
                        "first_name": call.from_user.first_name or "",
                        "username": call.from_user.username or "",
                        "is_bot": False,
                    },
                    "message": {
                        "message_id": call.message.message_id,
                        "chat": {"id": call.message.chat.id, "type": "private"},
                        "text": call.message.text or "",
                    },
                    "data": call.data,
                }
            }
            resp = requests.post(
                f"{API_BASE}/bot/webhook",
                json=update_payload,
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning(f"SMS callback forward returned {resp.status_code}: {resp.text}")
                bot.answer_callback_query(call.id, "⚠️ Ошибка. Попробуйте позже.")
        except Exception as e:
            log.warning(f"SMS callback forward error: {e}")
            bot.answer_callback_query(call.id, "⚠️ Ошибка. Попробуйте позже.")
        return

# ── Conversation: amount input ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: _state.get(m.from_user.id, {}).get("step") == "amount")
def on_amount(message):
    uid = message.from_user.id
    # Use smart parser — handles "4k", "50к", "25 тысяч", plain digits, etc.
    amount = _parse_amount_from_text(message.text or "")
    if not amount:
        bot.send_message(message.chat.id,
            "❌ Не удалось распознать сумму.\n"
            "Введите число, например: `50000`, `50к`, `4k`, `25 тысяч`",
            parse_mode="Markdown")
        return
    st       = _state[uid]
    tx_type  = st["data"]["type"]
    st["data"]["amount"] = amount
    st["step"] = "category"

    data = api("POST", "/api/auth", message.from_user)
    cats = (data.get("exp_cats" if tx_type == "expense" else "inc_cats") or []) if data else []
    if not cats:
        cats = [{"icon": "💰", "name": "Прочее"}]

    bot.send_message(
        message.chat.id,
        f"💰 *{fmt(amount)} сум*\n\nВыберите категорию:",
        reply_markup=cat_kb(cats)
    )

# ── Action helpers ────────────────────────────────────────────────────────────

def _begin_add(chat_id, uid):
    _state[uid] = {"step": "type", "data": {}}
    bot.send_message(chat_id, "➕ *Новая запись*\n\nЧто добавляем?", reply_markup=add_type_kb())

def _save_tx(call, st):
    d      = st["data"]
    today  = datetime.date.today().isoformat()
    now    = datetime.datetime.now().strftime("%H:%M")
    body   = {
        "type":      d["type"],
        "cat_icon":  d["cat_icon"],
        "cat_name":  d["cat_name"],
        "note":      d["cat_name"],
        "amount":    d["amount"],
        "date_str":  today,
        "time_str":  now,
        "currency":  "UZS",
        "recurring": False,
        "account":   "Наличка",
    }
    _state.pop(call.from_user.id, None)
    result = api("POST", "/api/transactions", call.from_user, body=body)
    sign   = "+" if d["type"] == "income" else "−"
    if result:
        xp_earned = result.get("xp_earned", 10)
        streak    = result.get("streak", 0)
        text = (
            f"✅ *Записано!*\n\n"
            f"{d['cat_icon']} *{d['cat_name']}*\n"
            f"*{sign}{fmt(d['amount'])} сум*\n\n"
            f"⭐ +{xp_earned} XP  ·  🔥 Серия: {streak} дн."
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=post_save_kb())
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=post_save_kb())
    else:
        try:
            bot.edit_message_text(
                "❌ Ошибка сохранения. Попробуйте через приложение.",
                call.message.chat.id, call.message.message_id
            )
        except Exception:
            pass

def _show_balance(chat_id, tg_user):
    data = api("POST", "/api/auth", tg_user)
    if not data:
        bot.send_message(chat_id, "❌ Не удалось получить данные. Попробуйте позже.")
        return
    txs       = data.get("txs", [])
    cur_month = datetime.date.today().strftime("%Y-%m")
    month_txs = [t for t in txs if str(t.get("dateStr", "")).startswith(cur_month)]
    income    = sum(t["amount"] for t in month_txs if t["type"] == "income")
    expense   = sum(t["amount"] for t in month_txs if t["type"] == "expense")
    total     = sum(t["amount"] if t["type"] == "income" else -t["amount"] for t in txs)
    diff      = income - expense
    arrow     = "📈" if diff >= 0 else "📉"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Добавить", callback_data="do:add"),
        InlineKeyboardButton("🧾 Операции", callback_data="do:recent"),
    )
    bot.send_message(chat_id,
        f"💳 *Баланс*\n\n"
        f"Всего: *{fmt(abs(total))} сум*\n\n"
        f"*Этот месяц:*\n"
        f"💚 Доходы: *+{fmt(income)} сум*\n"
        f"❤️ Расходы: *−{fmt(expense)} сум*\n"
        f"{arrow} Итог: *{'+' if diff>=0 else '−'}{fmt(abs(diff))} сум*",
        reply_markup=kb
    )

def _show_recent(chat_id, tg_user):
    data = api("POST", "/api/auth", tg_user)
    if not data:
        bot.send_message(chat_id, "❌ Не удалось получить данные.")
        return
    txs = data.get("txs", [])[:5]
    if not txs:
        bot.send_message(chat_id, "📭 Операций пока нет. Добавьте первую через /add")
        return
    lines = ["🧾 *Последние операции:*\n"]
    for t in txs:
        sign = "+" if t["type"] == "income" else "−"
        lines.append(f"{t['cat']} *{sign}{fmt(t['amount'])} сум*  _{t['catName']}_\n   _{t['dateStr']}  {t['time']}_\n")
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("➕ Добавить", callback_data="do:add"),
        InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL)),
    )
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)

def _show_stats(chat_id, uid):
    user = get_user(uid)
    if not user:
        bot.send_message(chat_id, "Откройте приложение через /start, чтобы зарегистрироваться.")
        return
    _lvl = user["level"] or 1
    _xp_base = (_lvl - 1) * 500
    _xp_for_next = _lvl * 500
    bar_filled = min(round((user["xp"] - _xp_base) / 500 * 10), 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    bot.send_message(chat_id,
        f"📊 *Ваша статистика*\n\n"
        f"🔥 Серия: *{user['streak']}* дн.  (рекорд: *{user['streak_record']}*)\n"
        f"⭐ XP: *{user['xp']}*\n"
        f"🏅 Уровень: *{user['level']}*\n\n"
        f"`{bar}` {user['xp'] - _xp_base} / 500 XP (до ур. {_lvl+1})",
        reply_markup=main_kb()
    )

def _show_budgets(chat_id, tg_user):
    data = api("POST", "/api/auth", tg_user)
    if not data:
        bot.send_message(chat_id, "❌ Не удалось получить данные.")
        return
    budgets = data.get("budgets", [])
    if not budgets:
        bot.send_message(chat_id,
            "🎯 Бюджетов пока нет.\nДобавьте их в приложении.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL))
            ]])
        )
        return
    lines = ["🎯 *Бюджеты — " + datetime.date.today().strftime("%B").capitalize() + ":*\n"]
    for b in budgets:
        limit   = max(b.get("limit", 1), 1)
        spent   = b.get("spent", 0)
        pct     = min(round(spent / limit * 100), 100)
        filled  = round(pct / 10)
        bar     = "█" * filled + "░" * (10 - filled)
        status  = "🔴" if pct >= 90 else "🟡" if pct >= 70 else "🟢"
        lines.append(
            f"{b['icon']} *{b['name']}*\n"
            f"`{bar}` {pct}%\n"
            f"_{fmt(spent)} / {fmt(limit)} сум_ {status}\n"
        )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL)))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)

def _build_report_text(uid: int, first_name: str) -> str:
    """Build a monthly summary report for a user."""
    data = api_with_id("POST", "/api/auth", uid, first_name)
    if not data:
        return ""
    txs       = data.get("txs", [])
    cur_month = datetime.date.today().strftime("%Y-%m")
    month_txs = [t for t in txs if str(t.get("dateStr", "")).startswith(cur_month)]
    income    = sum(t["amount"] for t in month_txs if t["type"] == "income")
    expense   = sum(t["amount"] for t in month_txs if t["type"] == "expense")
    diff      = income - expense
    arrow     = "📈" if diff >= 0 else "📉"
    # Top expense categories
    from collections import Counter
    cat_totals: Counter = Counter()
    for t in month_txs:
        if t["type"] == "expense":
            cat_totals[f"{t['cat']} {t['catName']}"] += t["amount"]
    top_cats = cat_totals.most_common(3)
    lines = [
        f"📊 *Отчёт за {datetime.date.today().strftime('%B').capitalize()}*\n",
        f"💚 Доходы:  *+{fmt(income)} сум*",
        f"❤️ Расходы: *−{fmt(expense)} сум*",
        f"{arrow} Итог: *{'+' if diff>=0 else '−'}{fmt(abs(diff))} сум*",
    ]
    if top_cats:
        lines.append("\n📌 *Топ расходов:*")
        for cat, amt in top_cats:
            lines.append(f"  {cat}: *{fmt(amt)} сум*")
    user_info = data.get("user", {})
    lines.append(f"\n🔥 Серия: *{user_info.get('streak',0)}* дн.  ·  ⭐ *{user_info.get('xp',0)}* XP")
    return "\n".join(lines)

# ── Scheduler jobs ────────────────────────────────────────────────────────────

def check_and_send_notifications():
    """Runs every minute. Sends daily reminders and auto-reports to users."""
    now_uzt      = datetime.datetime.now(UZT)
    current_time = now_uzt.strftime("%H:%M")
    today_str    = now_uzt.strftime("%Y-%m-%d")
    weekday      = now_uzt.weekday()   # 0=Mon … 6=Sun
    day_of_month = now_uzt.day

    # Purge old sent-set entries at midnight
    if current_time == "00:00":
        _notif_sent.clear()

    users = get_all_users_for_notify()
    for user in users:
        uid  = int(user["id"])
        name = user["first_name"] or "друг"

        # ── Daily reminder ──
        try:
            pref = json.loads(user.get("notif_pref_json") or "{}")
            if pref.get("enabled"):
                for t in pref.get("times", []):
                    key = (uid, today_str, f"remind:{t}")
                    if t == current_time and key not in _notif_sent:
                        _notif_sent.add(key)
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL))
                        ]])
                        streak = user.get("streak", 0)
                        last_log = user.get("last_log_date")
                        # Check if user hasn't logged today
                        logged_today = str(last_log) == today_str if last_log else False
                        if not logged_today and streak > 1:
                            # Streak at risk — urgent message
                            msg = (
                                f"🔥 *{name}, твоя серия под угрозой!*\n\n"
                                f"У тебя *{streak} дней подряд* — и они сгорят, если не записать сегодня.\n\n"
                                f"Добавь хотя бы одну запись и сохрани серию! ⚡"
                            )
                        elif not logged_today and streak == 1:
                            msg = (
                                f"💡 *{name}, не забудь записать расходы!*\n\n"
                                f"🔥 Начни новую серию — первый день уже идёт!"
                            )
                        else:
                            msg = (
                                f"💡 *{name}, не забудь записать расходы!*\n\n"
                                f"Ежедневный учёт — ключ к финансовой свободе 💪"
                            )
                        bot.send_message(uid, msg, reply_markup=kb)
        except Exception as e:
            log.warning(f"Reminder error uid={uid}: {e}")

        # ── Auto report ──
        try:
            rpref = json.loads(user.get("report_pref_json") or "{}")
            if rpref.get("enabled"):
                freq        = rpref.get("freq", "weekly")
                report_time = rpref.get("time", "20:00")
                should_send = False
                if   freq == "daily"   and current_time == report_time:
                    should_send = True
                elif freq == "weekly"  and weekday == 6 and current_time == report_time:
                    should_send = True
                elif freq == "monthly" and day_of_month == 1 and current_time == report_time:
                    should_send = True
                key = (uid, today_str, f"report:{freq}")
                if should_send and key not in _notif_sent:
                    _notif_sent.add(key)
                    text = _build_report_text(uid, name)
                    if text:
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL))
                        ]])
                        bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            log.warning(f"Report error uid={uid}: {e}")

def check_debt_reminders():
    """Runs once a day at 09:00. Reminds about debts due in 1 or 3 days."""
    try:
        conn = get_conn()
        today = datetime.date.today()
        in_1  = (today + datetime.timedelta(days=1)).isoformat()
        in_3  = (today + datetime.timedelta(days=3)).isoformat()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sd.*, u.first_name
                FROM social_debts sd
                JOIN users u ON sd.creator_id = u.id
                WHERE sd.status IN ('pending','approved')
                  AND sd.due_date IN (%s, %s)
            """, (in_1, in_3))
            debts = cur.fetchall()
        conn.close()
        for d in debts:
            days_left = 1 if str(d["due_date"]) == in_1 else 3
            emoji = "⚠️" if days_left == 1 else "📅"
            text = (
                f"{emoji} *Напоминание о долге*\n\n"
                f"{'Вы должны' if d['debt_type']=='borrowed' else 'Вам должны'} "
                f"*{fmt(d['amount'])} сум*\n"
                f"Срок: через *{days_left}* {'день' if days_left==1 else 'дня'}\n"
                f"{'📝 ' + d['note'] if d['note'] else ''}"
            )
            try:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL))
                ]])
                bot.send_message(d["creator_id"], text, reply_markup=kb)
                if d["counterpart_id"]:
                    bot.send_message(d["counterpart_id"], text, reply_markup=kb)
            except Exception as e:
                log.warning(f"Debt reminder send error: {e}")
    except Exception as e:
        log.error(f"check_debt_reminders error: {e}")

def check_donation_reminders():
    """Runs on the 1st of each month at 10:00. Reminds active pledgers who
    haven't self-reported a donation yet this period."""
    try:
        conn = get_conn()
        this_period = datetime.date.today().strftime("%Y-%m")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.user_id, p.monthly_amount
                FROM donation_pledges p
                WHERE p.active = TRUE
                  AND NOT EXISTS (
                      SELECT 1 FROM donations d
                      WHERE d.user_id = p.user_id AND d.status='confirmed' AND d.period=%s
                  )
            """, (this_period,))
            pledgers = cur.fetchall()
        conn.close()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💜 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL + "?page=donate"))
        ]])
        for p in pledgers:
            text = (
                f"💜 *Пора пожертвовать на этот месяц*\n\n"
                f"Твой план: *{fmt(p['monthly_amount'])} сум/мес*\n"
                f"Переведи любую сумму и отметь в Finly — всё идёт на благотворительность 🙏"
            )
            try:
                bot.send_message(p["user_id"], text, reply_markup=kb, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Donation reminder send error for {p['user_id']}: {e}")
    except Exception as e:
        log.error(f"check_donation_reminders error: {e}")

# ── Voice log ────────────────────────────────────────────────────────────────

# Russian/Uzbek numeric words → digits
_NUM_WORDS = {
    # Russian
    "один":1,"одна":1,"два":2,"две":2,"три":3,"четыре":4,"пять":5,
    "шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,
    "одиннадцать":11,"двенадцать":12,"тринадцать":13,"четырнадцать":14,
    "пятнадцать":15,"шестнадцать":16,"семнадцать":17,"восемнадцать":18,
    "девятнадцать":19,"двадцать":20,"тридцать":30,"сорок":40,
    "пятьдесят":50,"шестьдесят":60,"семьдесят":70,"восемьдесят":80,
    "девяносто":90,"сто":100,"двести":200,"триста":300,"четыреста":400,
    "пятьсот":500,"шестьсот":600,"семьсот":700,"восемьсот":800,"девятьсот":900,
    "тысяч":1000,"тысячу":1000,"тысячи":1000,"тысяча":1000,
    "миллион":1_000_000,"миллиона":1_000_000,"миллионов":1_000_000,
    # Uzbek
    "bir":1,"ikki":2,"uch":3,"to'rt":4,"tort":4,"besh":5,
    "olti":6,"yetti":7,"sakkiz":8,"to'qqiz":9,"toqqiz":9,"o'n":10,"on":10,
    "yigirma":20,"o'ttiz":30,"qirq":40,"ellik":50,"oltmish":60,
    "yetmish":70,"sakson":80,"to'qson":90,"yuz":100,"ming":1000,"mln":1_000_000,
}
_MULTIPLIERS = {1000, 1_000_000}

def _parse_amount_from_text(text: str) -> int | None:
    """Extract amount in sum from text. Handles '4k','50к','25 тысяч','4 ming', plain digits, word numbers."""
    text = text.lower().strip()
    # Step 1: N followed by shorthand multipliers (k, к, тысяч, ming, млн, mln, м)
    for pattern, mult in [
        (r"(\d[\d\s,]*)\s*тысяч[а-я]*",   1000),
        (r"(\d[\d\s,]*)\s*минг",            1000),
        (r"(\d[\d\s,]*)\s*ming",            1000),
        (r"(\d[\d\s,]*)к(?!\w)",            1000),   # "50к" — Cyrillic к, not followed by word char
        (r"(\d[\d\s,]*)k(?![а-яёА-ЯЁa-z])", 1000), # "4k" — Latin k, not followed by letters
        (r"(\d[\d\s,]*)\s*миллион[а-я]*",  1_000_000),
        (r"(\d[\d\s,]*)\s*млн",            1_000_000),
        (r"(\d[\d\s,]*)\s*mln",            1_000_000),
    ]:
        m = re.search(pattern, text)
        if m:
            base_str = m.group(1).replace(" ", "").replace(",", "")
            try:
                return int(base_str) * mult
            except ValueError:
                continue
    # Step 2: plain digits (including space-separated like "50 000")
    digits = re.findall(r"\d+", text)
    if digits:
        # If consecutive digit groups form a large number (e.g. "50 000"), join them
        combined = "".join(digits)
        return int(combined)
    # Step 3: word numbers (пятьдесят тысяч, etc.)
    words = re.findall(r"[a-zA-Zа-яёА-ЯЁ']+", text)
    total, current = 0, 0
    for w in words:
        v = _NUM_WORDS.get(w)
        if v is None:
            continue
        if v in _MULTIPLIERS:
            current = (current or 1) * v
            total += current
            current = 0
        else:
            current += v
    total += current
    return total if total > 0 else None

# Category keyword map (ru + uz keywords → category icon+name)
_VOICE_CATS = [
    (["продукт","еда","едa","food","oziq","ozuq","supermarket","супермаркет","гастроном"],       "🛒","Продукты"),
    (["кафе","ресторан","cafe","restoran","обед","lunch","ужин","завтрак","taom"],               "☕","Кафе/Рестораны"),
    (["транспорт","такси","bus","автобус","метро","metro","yol","avtobus","taxi"],               "🚌","Транспорт"),
    (["телефон","интернет","связь","telefon","internet","aloqa"],                                "📱","Связь"),
    (["аренда","квартира","дом","ijara","uy","rent"],                                           "🏠","Аренда"),
    (["здоровье","лекарство","аптека","dori","dorixona","shifoxona","врач","clinic"],           "💊","Здоровье"),
    (["одежда","kiyim","обувь","магазин","shop"],                                               "👕","Одежда"),
    (["зарплата","salary","maosh","доход","income"],                                            "💵","Зарплата"),
    (["развлечения","кино","cinema","entertainment","oyin","razvlecheniya"],                    "🎉","Развлечения"),
    (["коммунал","свет","газ","вода","gaz","suv","kommunal"],                                  "⚡","Коммуналка"),
]

def _parse_category(text: str) -> tuple[str, str] | tuple[None, None]:
    """Return (icon, name) best matching the text, or (None, None)."""
    text = text.lower()
    for keywords, icon, name in _VOICE_CATS:
        for kw in keywords:
            if kw in text:
                return icon, name
    return None, None

def _parse_tx_type(text: str) -> str:
    """Detect 'income' or 'expense' from text (default expense)."""
    income_kws = ["доход","зарплата","получил","oldi","maosh","salary","income","daromad","kirim","пришло"]
    t = text.lower()
    for kw in income_kws:
        if kw in t:
            return "income"
    return "expense"

def _voice_confirm_kb(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Верно",     callback_data=f"vc:yes:{uid}"),
        InlineKeyboardButton("✏️ Изменить", callback_data=f"vc:edit:{uid}"),
        InlineKeyboardButton("❌ Отмена",   callback_data=f"vc:cancel:{uid}"),
    )
    return kb

@bot.message_handler(content_types=["voice"])
def on_voice(message):
    uid = message.from_user.id
    if not _stt_client:
        bot.send_message(message.chat.id,
            "🎤 Голосовой ввод временно недоступен. Напишите запись текстом или используйте кнопки ниже.",
            reply_markup=main_kb())
        return

    # Show "typing" indicator
    bot.send_chat_action(message.chat.id, "typing")

    # Download voice file from Telegram
    try:
        file_info = bot.get_file(message.voice.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        log.error(f"Voice download error: {e}")
        bot.send_message(message.chat.id, "❌ Не удалось скачать голосовое сообщение. Попробуйте ещё раз.")
        return

    # Transcribe — Groq (fast) or OpenAI Whisper
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as audio_file:
            if _stt_provider == "groq":
                transcript = _stt_client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=audio_file,
                    response_format="text",
                    prompt="финансы, расходы, доходы, сумма, сум, тысяч, минг, maosh, xarajat",
                )
                text = (transcript if isinstance(transcript, str) else transcript.text).strip()
            else:
                transcript = _stt_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language=None,
                    prompt="финансы, расходы, доходы, сумма, сум, тысяч, минг, maosh, xarajat",
                )
                text = transcript.text.strip()
        os.unlink(tmp_path)
    except Exception as e:
        log.error(f"STT transcription error ({_stt_provider}): {e}")
        bot.send_message(message.chat.id, "❌ Не удалось распознать голос. Попробуйте ещё раз или введите текстом.")
        return

    if not text:
        bot.send_message(message.chat.id, "❌ Ничего не распознано. Попробуйте говорить чётче.")
        return

    # Parse amount + category
    amount = _parse_amount_from_text(text)
    cat_icon, cat_name = _parse_category(text)
    tx_type = _parse_tx_type(text)

    # Store pending voice transaction in state
    _state[uid] = {
        "step": "voice_confirm",
        "data": {
            "raw_text": text,
            "type": tx_type,
            "amount": amount,
            "cat_icon": cat_icon or "💬",
            "cat_name": cat_name or "Прочее",
        }
    }

    # Format the preview for user
    type_emoji = "💵" if tx_type == "income" else "💸"
    amount_str = f"{amount:,}".replace(",", " ") + " сум" if amount else "❓ сумма не определена"
    cat_str = f"{cat_icon} {cat_name}" if cat_icon else "❓ категория не определена"

    confirm_text = (
        f"🎤 *Распознано:* _{text}_\n\n"
        f"{type_emoji} *Тип:* {'Расход' if tx_type == 'expense' else 'Доход'}\n"
        f"💰 *Сумма:* {amount_str}\n"
        f"📂 *Категория:* {cat_str}\n\n"
        f"Всё верно?"
    )
    bot.send_message(message.chat.id, confirm_text, reply_markup=_voice_confirm_kb(uid), parse_mode="Markdown")


# ── Voice confirm callbacks ────────────────────────────────────────────────────

def _safe_answer(callback_id: str, text: str = ""):
    """Answer a callback query safely — never raises even if already answered."""
    try:
        bot.answer_callback_query(callback_id, text)
    except Exception:
        pass

def _safe_edit(call, text: str, reply_markup=None, parse_mode: str = "Markdown"):
    """Edit the original message; fall back to a new message if edit fails."""
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=reply_markup, parse_mode=parse_mode
        )
    except Exception:
        try:
            bot.send_message(call.message.chat.id, text,
                             reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass

def _handle_voice_confirm(call, uid: int):
    st = _state.get(uid, {})
    if st.get("step") != "voice_confirm":
        _safe_answer(call.id)
        _safe_edit(call, "⏱ Сессия истекла — отправьте запись снова.", reply_markup=None, parse_mode=None)
        return

    _safe_answer(call.id)           # answer exactly once, right here
    d = st["data"]

    if not d.get("amount"):
        _state[uid] = {"step": "amount", "data": {"type": d.get("type", "expense")}}
        _safe_edit(call,
            f"✏️ Введите сумму цифрами:\n\nКатегория: {d.get('cat_icon','💬')} {d.get('cat_name','Прочее')}",
            reply_markup=None)
        return

    today  = datetime.date.today().isoformat()
    now    = datetime.datetime.now().strftime("%H:%M")
    body   = {
        "type":      d["type"],
        "cat_icon":  d.get("cat_icon", "💬"),
        "cat_name":  d.get("cat_name", "Прочее"),
        "note":      d.get("raw_text", d.get("cat_name", ""))[:80],
        "amount":    int(d["amount"]),
        "date_str":  today,
        "time_str":  now,
        "currency":  "UZS",
        "recurring": False,
        "account":   "Наличка",
    }
    _state.pop(uid, None)
    log.info(f"[voice_confirm] uid={uid} body={body}")
    result = api("POST", "/api/transactions", call.from_user, body=body)
    log.info(f"[voice_confirm] result={result}")
    sign   = "+" if d["type"] == "income" else "−"
    if result:
        xp_earned = result.get("xp_earned", 10)
        streak    = result.get("streak", 0)
        _safe_edit(call,
            f"✅ *Записано!*\n\n"
            f"{d.get('cat_icon','💬')} *{d.get('cat_name','Прочее')}*\n"
            f"*{sign}{fmt(d['amount'])} сум*\n\n"
            f"⭐ +{xp_earned} XP  ·  🔥 Серия: {streak} дн.",
            reply_markup=post_save_kb())
    else:
        _safe_edit(call, "❌ Ошибка сохранения. Попробуйте ещё раз.", reply_markup=None)

def _handle_voice_edit(call, uid: int):
    """User wants to correct — restart from amount step."""
    _safe_answer(call.id)           # answer exactly once
    st = _state.get(uid, {})
    d  = st.get("data", {})
    tx_type = d.get("type", "expense")
    _state[uid] = {"step": "amount", "data": {"type": tx_type}}
    type_lbl = "Расход" if tx_type == "expense" else "Доход"
    _safe_edit(call,
        f"✏️ *{type_lbl}* — введите сумму:\n\nПримеры: `50000`, `50к`, `4k`, `25 тысяч`",
        reply_markup=None)

def _handle_voice_cancel(call, uid: int):
    _safe_answer(call.id, "Отменено")   # answer exactly once
    _state.pop(uid, None)
    _safe_edit(call, "❌ Отменено.", reply_markup=None, parse_mode=None)


# ── Smart free-text shortcut ──────────────────────────────────────────────────
# Catches messages like "4k кафе", "50к продукты", "100к зарплата" when user
# is NOT in any active multi-step state. Parses amount + category and shows
# the same voice-confirm flow for quick confirmation.

def _looks_like_bank_sms(text: str) -> bool:
    """Return True if text looks like a bank notification (HUMOcardbot, Uzcard, etc.)."""
    t = text.upper()
    if "HUMOCARD" in t or "UZCARD" in t:
        return True
    if re.search(r'[➕➖]', text) and "UZS" in t:
        return True
    if any(kw in t for kw in ["KAPITALBANK", "HAMKORBANK", "TBC BANK", "ALIF BANK"]):
        return True
    return False

def _looks_like_tx(text: str) -> bool:
    """Return True if text looks like a transaction shorthand (has a number + keyword).
    REQUIRES actual digits to be present — prevents false matches on pure text."""
    t = text.lower().strip()
    if t.startswith("/"):
        return False
    # Bank SMS messages are handled separately
    if _looks_like_bank_sms(text):
        return False
    # MUST contain at least one digit — no digits → not a transaction shorthand
    if not re.search(r"\d", t):
        return False
    # Must also contain at least one non-numeric word (the category part)
    _numeric_only = {"тысяч","тысячи","тысячу","тысяча","миллион","миллиона","миллионов",
                     "ming","минг","млн","mln","мин","lakh","lac"}
    words = [w for w in re.findall(r"[а-яёА-ЯЁa-zA-Z]{2,}", t) if w not in _numeric_only]
    return len(words) > 0

@bot.message_handler(func=lambda m: m.text and _looks_like_bank_sms(m.text))
def on_bank_sms(message):
    """Forward bank SMS messages to the FastAPI handler which has the full parser."""
    uid = message.from_user.id
    try:
        update_payload = {
            "message": {
                "message_id": message.message_id,
                "from": {
                    "id": uid,
                    "first_name": getattr(message.from_user, "first_name", "") or "",
                    "username": getattr(message.from_user, "username", "") or "",
                    "is_bot": False,
                },
                "chat": {"id": message.chat.id, "type": "private"},
                "text": message.text,
                "date": message.date,
            }
        }
        # Mark as forward if it is one
        if getattr(message, "forward_date", None):
            update_payload["message"]["forward_origin"] = {"type": "forwarded"}
        resp = requests.post(f"{API_BASE}/bot/webhook", json=update_payload, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Bank SMS forward returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Bank SMS forward error: {e}")

@bot.message_handler(func=lambda m: (
    m.text and
    not _state.get(m.from_user.id) and
    _looks_like_tx(m.text)
))
def on_quick_tx(message):
    """Parse 'amount + category' from a single free-text message."""
    uid  = message.from_user.id
    text = message.text.strip()

    amount   = _parse_amount_from_text(text)
    cat_icon, cat_name = _parse_category(text)
    tx_type  = _parse_tx_type(text)

    if not amount:
        # Couldn't parse — show the normal add menu
        _begin_add(message.chat.id, uid)
        return

    # Store in voice-confirm state (reuse that flow)
    _state[uid] = {
        "step": "voice_confirm",
        "data": {
            "raw_text": text,
            "type":     tx_type,
            "amount":   amount,
            "cat_icon": cat_icon or "💬",
            "cat_name": cat_name or "Прочее",
        }
    }

    sign      = "+" if tx_type == "income" else "−"
    type_lbl  = "Доход" if tx_type == "income" else "Расход"
    amt_str   = fmt(amount)
    cat_str   = f"{cat_icon} {cat_name}" if cat_icon else "💬 Прочее"

    bot.send_message(
        message.chat.id,
        f"⚡ *Быстрая запись*\n\n"
        f"{'💵' if tx_type == 'income' else '💸'} *Тип:* {type_lbl}\n"
        f"💰 *Сумма:* {sign}{amt_str} сум\n"
        f"📂 *Категория:* {cat_str}\n\n"
        f"Всё верно?",
        reply_markup=_voice_confirm_kb(uid),
        parse_mode="Markdown"
    )


# ── Chat Automation (Secretary / Business Bot) handlers ──────────────────────

@bot.business_connection_handler()
def on_business_connection(bc):
    """User connects or disconnects Finly via Telegram Chat Automation."""
    try:
        update_payload = {"business_connection": {
            "id": bc.id,
            "user": {
                "id": bc.user.id,
                "first_name": getattr(bc.user, "first_name", "") or "",
                "username": getattr(bc.user, "username", "") or "",
            },
            "user_chat_id": getattr(bc, "user_chat_id", bc.user.id),
            "date": getattr(bc, "date", 0),
            "can_reply": getattr(bc, "can_reply", False),
            "is_enabled": getattr(bc, "is_enabled", True),
        }}
        requests.post(f"{API_BASE}/bot/webhook", json=update_payload, timeout=10)
    except Exception as e:
        log.warning(f"Business connection forward error: {e}")

@bot.business_message_handler(func=lambda m: True)
def on_business_message(message):
    """Forward Chat Automation messages to FastAPI for bank SMS parsing."""
    try:
        update_payload = {"business_message": {
            "message_id": message.message_id,
            "from": {
                "id": message.from_user.id if message.from_user else 0,
                "is_bot": getattr(message.from_user, "is_bot", False),
                "first_name": getattr(message.from_user, "first_name", "") or "",
            },
            "chat": {"id": message.chat.id, "type": getattr(message.chat, "type", "private")},
            "text": message.text or "",
            "date": message.date,
            "business_connection_id": getattr(message, "business_connection_id", ""),
        }}
        requests.post(f"{API_BASE}/bot/webhook", json=update_payload, timeout=10)
    except Exception as e:
        log.warning(f"Business message forward error: {e}")

# ── Startup ───────────────────────────────────────────────────────────────────

def start_api():
    import uvicorn
    log.info(f"Starting FastAPI on port {API_PORT}")
    uvicorn.run("api.main:app", host="0.0.0.0", port=API_PORT, log_level="info")

if __name__ == "__main__":
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()
    time.sleep(2)

    # ── Scheduler ──
    scheduler = BackgroundScheduler(timezone=UZT)
    # Check reminders & reports every minute
    scheduler.add_job(check_and_send_notifications, "cron", minute="*",
                      id="notif_check", max_instances=1, coalesce=True)
    # Debt due-date reminders every day at 09:00 UZT
    scheduler.add_job(check_debt_reminders, "cron", hour=9, minute=0,
                      id="debt_reminders", max_instances=1)
    # Donation reminders on the 1st of each month at 10:00 UZT
    scheduler.add_job(check_donation_reminders, "cron", day=1, hour=10, minute=0,
                      id="donation_reminders", max_instances=1)
    scheduler.start()
    log.info("Scheduler started")

    log.info("Starting bot polling...")
    try:
        bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared")
    except Exception as e:
        log.warning(f"Could not clear webhook: {e}")

    _bot_token = os.getenv("BOT_TOKEN", "")

    # Set menu button to open webapp
    if WEBAPP_URL and _bot_token:
        try:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{_bot_token}/setChatMenuButton",
                json={"menu_button": {"type": "web_app", "text": "💰 Finly", "web_app": {"url": WEBAPP_URL}}},
                timeout=10,
            )
            log.info(f"Menu button set to {WEBAPP_URL}")
        except Exception as e:
            log.warning(f"Could not set menu button: {e}")

    # Set bot description shown before user presses Start (Russian)
    if _bot_token:
        try:
            import requests as _req
            _req.post(
                f"https://api.telegram.org/bot{_bot_token}/setMyDescription",
                json={
                    "description": (
                        "💰 Finly — умный трекер личных финансов\n\n"
                        "• Записывай доходы и расходы за секунды\n"
                        "• Голосовой ввод и быстрые ярлыки iOS\n"
                        "• Отчёты, графики и аналитика\n"
                        "• Цели, долги и копилки\n"
                        "• XP, уровни и серии — финансы как игра 🎮\n\n"
                        "Нажми «Начать» и открой приложение 👇"
                    ),
                    "language_code": "ru"
                },
                timeout=10,
            )
            # Also set short description (shown in search)
            _req.post(
                f"https://api.telegram.org/bot{_bot_token}/setMyShortDescription",
                json={"short_description": "Умный трекер финансов — расходы, цели, отчёты и XP 💰", "language_code": "ru"},
                timeout=10,
            )
            log.info("Bot description set (RU)")
        except Exception as e:
            log.warning(f"Could not set bot description: {e}")

    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=20,
        logger_level=logging.ERROR,
        allowed_updates=[
            "message", "callback_query",
            "business_connection", "business_message",
            "edited_business_message", "deleted_business_messages",
        ],
    )
