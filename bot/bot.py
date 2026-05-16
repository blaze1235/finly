import os, logging, threading, time
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import psycopg2
import psycopg2.extras

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
API_PORT     = int(os.getenv("PORT", os.getenv("API_PORT", "8080")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("finly.bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

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

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid  = message.from_user.id
    name = message.from_user.first_name or "друг"
    user = get_user(uid)
    is_new = user is None
    text = (
        f"👋 Привет, *{name}*! Добро пожаловать в *Finly*.\n\n"
        f"Веди расходы каждый день, зарабатывай XP и соревнуйся с друзьями 🔥\n\n"
        f"Нажми кнопку ниже 👇"
        if is_new else
        f"👋 С возвращением, *{name}*!\n\n"
        f"🔥 Серия: *{user['streak']}* дней · ⭐ *{user['xp']}* XP\n\n"
        f"Продолжи серию — открой Finly 👇"
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Открыть Finly", web_app=WebAppInfo(url=WEBAPP_URL)))
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    user = get_user(message.from_user.id)
    if not user:
        bot.send_message(message.chat.id, "Сначала откройте приложение через /start.")
        return
    bot.send_message(message.chat.id,
        f"📊 *Ваша статистика*\n\n"
        f"🔥 Серия: *{user['streak']}* дней (рекорд: *{user['streak_record']}*)\n"
        f"⭐ XP: *{user['xp']}*\n"
        f"🏅 Уровень: *{user['level']}*"
    )

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id,
        "📖 *Finly — помощь*\n\n"
        "/start — открыть приложение\n"
        "/stats — ваша статистика\n"
    )

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
