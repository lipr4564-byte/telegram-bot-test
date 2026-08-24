import os
import time
import sqlite3
import threading
import requests
import telebot
from telebot import types
from datetime import datetime

# ─── Конфиг ───────────────────────────────────────────────────────────────────
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана на хостинге!")

bot = telebot.TeleBot(TOKEN)

# Админы бота — только они видят /admin и любые команды, все остальные
# полностью игнорируются (ни одного ответа обычным пользователям).
ADMIN_IDS = {7930482209}
env_admin = os.getenv('ADMIN_ID', '0')
if env_admin and env_admin != '0':
    ADMIN_IDS.add(int(env_admin))

# Группа/чат, куда бот шлёт логи (запуск, ошибки, действия админа)
LOG_CHAT_ID = -5288630698

DB_PATH = os.getenv('DB_PATH', 'bot_data.db')

# ─── База данных ──────────────────────────────────────────────────────────────
db_lock = threading.Lock()


def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db_lock, db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_deletions (
                chat_id INTEGER,
                message_id INTEGER,
                thread_id INTEGER,
                delete_at REAL,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY,
                type TEXT  -- 'rp' или 'offtop'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        """)
        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('deleted_total', 0)")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('rp_timer_seconds', '2400')")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_min_words', '1')")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_max_words', '10')")
        conn.commit()


def get_config(key, default=None):
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_config(key, value):
    with db_lock, db_conn() as conn:
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()


def get_topics(topic_type=None):
    with db_lock, db_conn() as conn:
        if topic_type:
            rows = conn.execute("SELECT topic_id FROM topics WHERE type=?", (topic_type,)).fetchall()
        else:
            rows = conn.execute("SELECT topic_id, type FROM topics").fetchall()
        return rows


def add_topic(topic_id, topic_type):
    with db_lock, db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO topics (topic_id, type) VALUES (?, ?)", (topic_id, topic_type))
        conn.commit()


def remove_topic(topic_id):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM topics WHERE topic_id=?", (topic_id,))
        conn.commit()


def bump_deleted(n=1):
    with db_lock, db_conn() as conn:
        conn.execute("UPDATE stats SET value = value + ? WHERE key='deleted_total'", (n,))
        conn.commit()


def get_deleted_total():
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT value FROM stats WHERE key='deleted_total'").fetchone()
        return row[0] if row else 0


def db_add_pending(chat_id, message_id, thread_id, delete_at):
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_deletions (chat_id, message_id, thread_id, delete_at) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, thread_id, delete_at)
        )
        conn.commit()


def db_remove_pending(chat_id, message_id):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM pending_deletions WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        conn.commit()


def db_all_pending():
    with db_lock, db_conn() as conn:
        return conn.execute("SELECT chat_id, message_id, thread_id, delete_at FROM pending_deletions").fetchall()


def db_clear_all_pending():
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT chat_id, message_id FROM pending_deletions").fetchall()
        conn.execute("DELETE FROM pending_deletions")
        conn.commit()
        return rows


# ─── Кэш конфига в памяти (обновляется при изменениях) ────────────────────────
init_db()  # таблицы должны существовать до первого чтения конфига ниже

cache_lock = threading.Lock()
RP_TIMER_SECONDS = int(get_config('rp_timer_seconds', 2400))
OFFTOP_MIN_WORDS = int(get_config('offtop_min_words', 1))
OFFTOP_MAX_WORDS = int(get_config('offtop_max_words', 10))
RP_TOPICS = set()
OFFTOP_TOPICS = set()


def reload_topics_cache():
    global RP_TOPICS, OFFTOP_TOPICS
    with cache_lock:
        RP_TOPICS = {t[0] for t in get_topics('rp')}
        OFFTOP_TOPICS = {t[0] for t in get_topics('offtop')}


def all_topics():
    with cache_lock:
        return RP_TOPICS | OFFTOP_TOPICS


# Активные потоковые таймеры: {(chat_id, message_id): Timer}
active_timers = {}
timers_lock = threading.Lock()

# Состояние ожидания ввода от админа: {admin_id: 'set_timer' | 'add_rp' | 'add_offtop' | 'remove_topic'}
awaiting_input = {}


# ─── Логирование в Telegram-группу ─────────────────────────────────────────────
def log_event(text):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}")
    try:
        bot.send_message(LOG_CHAT_ID, text, parse_mode='HTML')
    except Exception as e:
        print(f"  (не удалось отправить лог в группу: {e})")


# ─── Утилиты удаления ───────────────────────────────────────────────────────────
def _do_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
        bump_deleted()
        return True
    except Exception:
        return False
    finally:
        db_remove_pending(chat_id, message_id)
        with timers_lock:
            active_timers.pop((chat_id, message_id), None)


def schedule_delete(chat_id, message_id, thread_id, delay_seconds, persist=True):
    key = (chat_id, message_id)
    delete_at = time.time() + delay_seconds

    if persist:
        db_add_pending(chat_id, message_id, thread_id, delete_at)

    def _fire():
        _do_delete(chat_id, message_id)

    timer = threading.Timer(delay_seconds, _fire)
    timer.daemon = True
    with timers_lock:
        active_timers[key] = timer
    timer.start()


def cancel_timer(chat_id, message_id):
    key = (chat_id, message_id)
    with timers_lock:
        timer = active_timers.pop(key, None)
    if timer:
        timer.cancel()
    db_remove_pending(chat_id, message_id)


def restore_pending_on_startup():
    """При старте вычитываем очередь из БД: просроченное удаляем сразу,
    остальное — досчитываем таймер с оставшимся временем."""
    rows = db_all_pending()
    now = time.time()
    restored, fired_now = 0, 0

    for chat_id, message_id, thread_id, delete_at in rows:
        remaining = delete_at - now
        if remaining <= 0:
            _do_delete(chat_id, message_id)
            fired_now += 1
        else:
            schedule_delete(chat_id, message_id, thread_id, remaining, persist=False)
            restored += 1

    if rows:
        log_event(f"🔄 Восстановление после запуска: {restored} таймеров возобновлено, "
                   f"{fired_now} просроченных сообщений удалено сразу.")


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ─── Обработчики сообщений (работают для ВСЕХ пользователей — это чистка) ─────
CONTENT_TYPES = [
    'text', 'photo', 'video', 'animation',
    'sticker', 'voice', 'audio', 'document',
    'video_note', 'poll', 'location', 'contact',
]


@bot.message_handler(content_types=CONTENT_TYPES)
def handle_message(message):
    thread_id = message.message_thread_id
    if thread_id not in all_topics():
        return

    text = message.text or message.caption or ""

    if '#' in text:
        return

    if thread_id in RP_TOPICS:
        schedule_delete(message.chat.id, message.message_id, thread_id, RP_TIMER_SECONDS)
        return

    if thread_id in OFFTOP_TOPICS:
        word_count = len(text.split()) if text else 0
        if OFFTOP_MIN_WORDS <= word_count <= OFFTOP_MAX_WORDS:
            _do_delete(message.chat.id, message.message_id)


@bot.edited_message_handler(content_types=CONTENT_TYPES)
def handle_edit(message):
    thread_id = message.message_thread_id
    if thread_id not in all_topics():
        return

    text = message.text or message.caption or ""

    if '#' in text:
        cancel_timer(message.chat.id, message.message_id)
    else:
        if thread_id in RP_TOPICS:
            key = (message.chat.id, message.message_id)
            with timers_lock:
                already = key in active_timers
            if not already:
                schedule_delete(message.chat.id, message.message_id, thread_id, RP_TIMER_SECONDS)


# ─── Админ-панель ──────────────────────────────────────────────────────────────
def admin_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⏱ Изменить таймер RP", callback_data="set_timer"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="stats"),
    )
    kb.add(
        types.InlineKeyboardButton("🧹 Удалить всё сейчас", callback_data="purge_confirm"),
        types.InlineKeyboardButton("📋 Список тем", callback_data="list_topics"),
    )
    kb.add(
        types.InlineKeyboardButton("➕ РП-тема", callback_data="add_rp"),
        types.InlineKeyboardButton("➕ Офтоп-тема", callback_data="add_offtop"),
    )
    kb.add(
        types.InlineKeyboardButton("➖ Удалить тему", callback_data="remove_topic"),
    )
    return kb


@bot.message_handler(commands=['start', 'admin'])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return  # полное игнорирование обычных пользователей
    minutes = RP_TIMER_SECONDS // 60
    bot.send_message(
        message.chat.id,
        "🤖 <b>Админ-панель чистильщика</b>\n\n"
        f"🗂 РП-темы: <code>{sorted(RP_TOPICS) or '—'}</code>\n"
        f"🗑 Оффтоп-темы: <code>{sorted(OFFTOP_TOPICS) or '—'}</code>\n"
        f"⏱ Текущий таймер RP: <b>{minutes} мин</b>\n"
        f"📊 Удалено всего: <b>{get_deleted_total()}</b>",
        parse_mode='HTML',
        reply_markup=admin_menu_markup()
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return

    data = call.data
    admin_id = call.from_user.id

    if data == "stats":
        with timers_lock:
            pending = len(active_timers)
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"📊 <b>Статистика</b>\n"
            f"Удалено всего (с учётом рестартов): <b>{get_deleted_total()}</b>\n"
            f"В очереди прямо сейчас: <b>{pending}</b>",
            parse_mode='HTML'
        )

    elif data == "list_topics":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"📋 <b>Отслеживаемые темы</b>\n"
            f"РП (удаление через {RP_TIMER_SECONDS // 60} мин): <code>{sorted(RP_TOPICS) or '—'}</code>\n"
            f"Оффтоп (мгновенно, {OFFTOP_MIN_WORDS}-{OFFTOP_MAX_WORDS} слов): <code>{sorted(OFFTOP_TOPICS) or '—'}</code>",
            parse_mode='HTML'
        )

    elif data == "set_timer":
        awaiting_input[admin_id] = 'set_timer'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "⏱ Пришли новое значение таймера RP в <b>минутах</b> (например: 10)",
                          parse_mode='HTML')

    elif data == "add_rp":
        awaiting_input[admin_id] = 'add_rp'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "➕ Пришли ID темы, которую добавить в <b>РП</b>-список")

    elif data == "add_offtop":
        awaiting_input[admin_id] = 'add_offtop'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "➕ Пришли ID темы, которую добавить в <b>Оффтоп</b>-список")

    elif data == "remove_topic":
        awaiting_input[admin_id] = 'remove_topic'
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "➖ Пришли ID темы, которую нужно убрать из списков")

    elif data == "purge_confirm":
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Да, удалить всё", callback_data="purge_now"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="purge_cancel"),
        )
        bot.answer_callback_query(call.id)
        with timers_lock:
            pending = len(active_timers)
        bot.send_message(call.message.chat.id, f"⚠️ Удалить прямо сейчас {pending} сообщений из очереди?",
                          reply_markup=kb)

    elif data == "purge_now":
        bot.answer_callback_query(call.id, "Удаляю...")
        with timers_lock:
            timers_copy = dict(active_timers)
        for timer in timers_copy.values():
            timer.cancel()
        rows = db_clear_all_pending()
        count = 0
        for chat_id, message_id in rows:
            try:
                bot.delete_message(chat_id, message_id)
                count += 1
            except Exception:
                pass
        bump_deleted(count)
        with timers_lock:
            active_timers.clear()
        bot.send_message(call.message.chat.id, f"🧹 Удалено сообщений: {count}")
        log_event(f"🧹 Админ {admin_id} выполнил ручную очистку очереди: {count} сообщений.")

    elif data == "purge_cancel":
        bot.answer_callback_query(call.id, "Отменено")
        bot.send_message(call.message.chat.id, "Отменено.")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_input, content_types=['text'])
def handle_admin_input(message):
    admin_id = message.from_user.id
    action = awaiting_input.pop(admin_id, None)
    text = message.text.strip()

    if action == 'set_timer':
        try:
            minutes = float(text.replace(',', '.'))
            if minutes <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, "❌ Нужно положительное число минут. Попробуй ещё раз через /admin.")
            return
        global RP_TIMER_SECONDS
        RP_TIMER_SECONDS = int(minutes * 60)
        set_config('rp_timer_seconds', RP_TIMER_SECONDS)
        bot.send_message(message.chat.id, f"✅ Таймер RP-тем изменён на {minutes:g} мин.")
        log_event(f"⏱ Админ {admin_id} изменил таймер RP на {minutes:g} мин.")

    elif action in ('add_rp', 'add_offtop'):
        try:
            topic_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ ID темы должен быть числом.")
            return
        topic_type = 'rp' if action == 'add_rp' else 'offtop'
        add_topic(topic_id, topic_type)
        reload_topics_cache()
        bot.send_message(message.chat.id, f"✅ Тема {topic_id} добавлена в список «{topic_type}».")
        log_event(f"➕ Админ {admin_id} добавил тему {topic_id} в «{topic_type}».")

    elif action == 'remove_topic':
        try:
            topic_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ ID темы должен быть числом.")
            return
        remove_topic(topic_id)
        reload_topics_cache()
        bot.send_message(message.chat.id, f"✅ Тема {topic_id} убрана из списков.")
        log_event(f"➖ Админ {admin_id} убрал тему {topic_id}.")


# Любые другие команды от НЕ-админов — полностью игнорируются (без ответа)
@bot.message_handler(commands=['status', 'stats', 'topics', 'purge'])
def legacy_commands_redirect(message):
    if not is_admin(message.from_user.id):
        return
    cmd_admin(message)


# ─── Запуск ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    reload_topics_cache()

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Чистильщик запущен.")
    print(f"  РП-темы      : {sorted(RP_TOPICS)}")
    print(f"  Оффтоп-темы  : {sorted(OFFTOP_TOPICS)}")
    print(f"  Таймер RP    : {RP_TIMER_SECONDS // 60} мин")
    print(f"  Админы       : {ADMIN_IDS}")

    restore_pending_on_startup()
    log_event(f"🚀 Бот запущен. РП: {sorted(RP_TOPICS)} | Оффтоп: {sorted(OFFTOP_TOPICS)} | "
              f"Таймер: {RP_TIMER_SECONDS // 60} мин")

    RETRY_DELAYS = [5, 10, 30, 60]
    attempt = 0

    while True:
        try:
            attempt = 0
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=45,
            )
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(f"[{datetime.now():%H:%M:%S}] Сеть упала ({type(e).__name__}). Перезапуск через {delay}с...")
            time.sleep(delay)
            attempt += 1
        except KeyboardInterrupt:
            print(f"[{datetime.now():%H:%M:%S}] Остановлено вручную.")
            break
        except Exception as e:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(f"[{datetime.now():%H:%M:%S}] Ошибка: {e}. Перезапуск через {delay}с...")
            log_event(f"❌ Ошибка в polling: {e}. Перезапуск через {delay}с...")
            time.sleep(delay)
            attempt += 1
