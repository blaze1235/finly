import os, logging, threading, time, hmac, hashlib, json, urllib.parse
import datetime
import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton,
)
import psycopg2
import psycopg2.extras

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
API_PORT     = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))
API_BASE     = f"http://127.0.0.1:{API_PORT}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("finly.bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Per-user conversation state: {uid: {"step": str, "data": dict}}
_state: dict = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return "0"

def get_user(uid):
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            return cur.fetchone()
    except Exception as e:
        log.error(f"DB error: {e}")
        return None
    finally:
        try: conn.close()
        except: pass

def make_init_data(tg_user) -> str:
    """Build a valid X-Init-Data header value so the bot can call the API as the user."""
    user_obj = {
        "id": tg_user.id,
        "first_name": tg_user.first_name or "",
        "username": tg_user.username or "",
    }
    user_json  = json.dumps(user_obj, ensure_ascii=False, separators=(",", ":"))
    auth_date  = str(int(time.time()))
    # data_check_string = sorted key=value pairs (raw, not encoded), joined by \n
    params     = {"auth_date": auth_date, "user": user_json}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret     = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    hash_val   = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    # Values are URL-encoded in the header (API unquotes them on receipt)
    parts = [f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items()]
    parts.append(f"hash={hash_val}")
    return "&".join(parts)

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
        else:
            return None
        res.raise_for_status()
        return res.json()
    except Exception as e:
        log.error(f"API {method} {path}: {e}")
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
    user = get_user(uid)
    if user is None:
        text = (
            f"👋 Привет, *{name}*! Добро пожаловать в *Finly*.\n\n"
            f"Здесь ты ведёшь расходы, зарабатываешь XP и соревнуешься с друзьями 🔥\n\n"
            f"Открой приложение, чтобы начать 👇"
        )
    else:
        text = (
            f"👋 С возвращением, *{name}*!\n\n"
            f"🔥 Серия: *{user['streak']}* дн.  ·  ⭐ *{user['xp']}* XP  ·  🏅 Ур. *{user['level']}*\n\n"
            f"Что делаем?"
        )
    bot.send_message(message.chat.id, text, reply_markup=shortcut_kb())
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=main_kb())

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
        "/stats — серия и XP\n\n"
        "Или откройте мини-приложение для полного функционала 👆"
    )

# ── Inline-button dispatcher ──────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid  = call.from_user.id
    data = call.data

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

# ── Conversation: amount input ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: _state.get(m.from_user.id, {}).get("step") == "amount")
def on_amount(message):
    uid = message.from_user.id
    raw = "".join(c for c in message.text if c.isdigit())
    if not raw:
        bot.send_message(message.chat.id, "❌ Введите числовую сумму, например: `50000`")
        return
    amount   = int(raw)
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
        "account":   "Карта",
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
    bar_filled = min(round(user["xp"] / max(user["level"] * 500, 1) * 10), 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    bot.send_message(chat_id,
        f"📊 *Ваша статистика*\n\n"
        f"🔥 Серия: *{user['streak']}* дн.  (рекорд: *{user['streak_record']}*)\n"
        f"⭐ XP: *{user['xp']}*\n"
        f"🏅 Уровень: *{user['level']}*\n\n"
        f"`{bar}` {user['xp']} / {user['level']*500} XP",
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

# ── Startup ───────────────────────────────────────────────────────────────────

def start_api():
    import uvicorn
    log.info(f"Starting FastAPI on port {API_PORT}")
    uvicorn.run("api.main:app", host="0.0.0.0", port=API_PORT, log_level="info")

if __name__ == "__main__":
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()
    time.sleep(2)

    log.info("Starting bot polling...")
    try:
        bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook cleared")
    except Exception as e:
        log.warning(f"Could not clear webhook: {e}")

    # Do NOT use skip_pending=True — it calls __skip_updates() before the retry
    # loop starts, so a 409 from an overlapping Railway instance crashes the
    # whole process instead of being caught and retried.
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=20,
        logger_level=logging.ERROR,
    )
