#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import sqlite3
import threading
import http.server
import socketserver
from datetime import datetime
from typing import Set, Tuple, List, Optional, Dict, Any

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# =====================================================================
#  КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# =====================================================================

TOKEN = os.getenv('BOT_TOKEN', '').strip()

# ID главного администратора (только ты имеешь доступ к управлению)
ADMIN_IDS: Set[int] = {7930482209}

env_admin = os.getenv('ADMIN_ID', '').strip()
if env_admin:
    for part in env_admin.replace(',', ' ').replace(';', ' ').split():
        if part.lstrip('-').isdigit():
            ADMIN_IDS.add(int(part))

# Группа для отправки системных логов и отчетов об удалении
LOG_CHAT_ID: int = int(os.getenv('LOG_CHAT_ID', '-5288630698'))

# Путь к файлу базы данных SQLite
DB_PATH: str = os.getenv('DB_PATH', 'bot_data.db')

# Дефолтные темы из переменных окружения (на случай перезапусков контейнеров)
ENV_RP_TOPICS_RAW = os.getenv('RP_TOPICS', '').strip()
ENV_OFFTOP_TOPICS_RAW = os.getenv('OFFTOP_TOPICS', '').strip()

# Типы контента, которые отслеживает чистильщик в темах
CONTENT_TYPES = [
    'text', 'photo', 'video', 'animation',
    'sticker', 'voice', 'audio', 'document',
    'video_note', 'poll', 'location', 'contact',
]

# =====================================================================
#  ФОНОВЫЙ HTTP СЕРВЕР (ДЛЯ RENDER / RAILWAY / KOYEB)
# =====================================================================

def start_health_check_server():
    """
    Запускает простой HTTP-сервер на $PORT (если переменная задана хостингом).
    Это предотвращает цикличный рестарт бота каждые 60 секунд на Render/Railway.
    """
    port_str = os.getenv('PORT')
    if not port_str:
        return
    try:
        port = int(port_str)
        class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OK - Telegram Cleaner Bot is running.\n")
            def log_message(self, format, *args):
                pass

        server = socketserver.TCPServer(("", port), HealthCheckHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="HealthServer")
        t.start()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] HTTP Health-Check сервер запущен на порту {port}")
    except Exception as e:
        print(f"[WARN] Не удалось запустить Health-Check сервер: {e}")

# =====================================================================
#  ИНИЦИАЛИЗАЦИЯ БОТА И БАЗЫ ДАННЫХ (SQLite)
# =====================================================================

bot = telebot.TeleBot(TOKEN if TOKEN else "dummy_token", parse_mode=None, threaded=True)
db_lock = threading.Lock()


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    with db_lock, db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_deletions (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            delete_at REAL NOT NULL,
            created_at REAL NOT NULL,
            user_info TEXT,
            snippet TEXT,
            PRIMARY KEY (chat_id, message_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            topic_id INTEGER PRIMARY KEY,
            type TEXT NOT NULL  -- 'rp' (по таймеру) или 'offtop' (мгновенно)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        """)

        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('deleted_total', 0);")
        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('started_at', ?);", (int(time.time()),))
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('rp_timer_seconds', '2400');")  # 40 минут по умолчанию
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_min_words', '1');")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_max_words', '10');")

        if ENV_RP_TOPICS_RAW:
            for part in ENV_RP_TOPICS_RAW.replace(',', ' ').replace(';', ' ').split():
                if part.lstrip('-').isdigit():
                    conn.execute("INSERT OR IGNORE INTO topics (topic_id, type) VALUES (?, 'rp')", (int(part),))

        if ENV_OFFTOP_TOPICS_RAW:
            for part in ENV_OFFTOP_TOPICS_RAW.replace(',', ' ').replace(';', ' ').split():
                if part.lstrip('-').isdigit():
                    conn.execute("INSERT OR IGNORE INTO topics (topic_id, type) VALUES (?, 'offtop')", (int(part),))

        conn.commit()


init_db()

# =====================================================================
#  МЕТОДЫ РАБОТЫ С БАЗОЙ ДАННЫХ
# =====================================================================

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_config(key: str, value: Any):
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value))
        )
        conn.commit()


def get_topics_from_db(topic_type: Optional[str] = None) -> List[Tuple[int, ...]]:
    with db_lock, db_conn() as conn:
        if topic_type:
            return conn.execute("SELECT topic_id FROM topics WHERE type = ?", (topic_type,)).fetchall()
        return conn.execute("SELECT topic_id, type FROM topics ORDER BY topic_id ASC").fetchall()


def add_topic_to_db(topic_id: int, topic_type: str):
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO topics (topic_id, type) VALUES (?, ?)",
            (int(topic_id), topic_type)
        )
        conn.commit()


def remove_topic_from_db(topic_id: int):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM topics WHERE topic_id = ?", (int(topic_id),))
        conn.commit()


def bump_deleted_stat(count: int = 1):
    with db_lock, db_conn() as conn:
        conn.execute("UPDATE stats SET value = value + ? WHERE key = 'deleted_total'", (count,))
        conn.commit()


def get_deleted_stat() -> int:
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT value FROM stats WHERE key = 'deleted_total'").fetchone()
        return int(row[0]) if row else 0


def db_add_pending(chat_id: int, message_id: int, thread_id: Optional[int], delete_at: float, user_info: str = "", snippet: str = ""):
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_deletions (chat_id, message_id, thread_id, delete_at, created_at, user_info, snippet) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, thread_id, delete_at, time.time(), user_info, snippet)
        )
        conn.commit()


def db_remove_pending(chat_id: int, message_id: int):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM pending_deletions WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        conn.commit()


def db_all_pending() -> List[Tuple[int, int, Optional[int], float, str, str]]:
    with db_lock, db_conn() as conn:
        return conn.execute("SELECT chat_id, message_id, thread_id, delete_at, user_info, snippet FROM pending_deletions ORDER BY delete_at ASC").fetchall()


def db_count_pending() -> int:
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()
        return int(row[0]) if row else 0


def db_get_expired_pending(now_ts: float) -> List[Tuple[int, int, Optional[int], str, str]]:
    with db_lock, db_conn() as conn:
        return conn.execute("SELECT chat_id, message_id, thread_id, user_info, snippet FROM pending_deletions WHERE delete_at <= ?", (now_ts,)).fetchall()


def db_clear_all_pending() -> List[Tuple[int, int]]:
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT chat_id, message_id FROM pending_deletions").fetchall()
        conn.execute("DELETE FROM pending_deletions")
        conn.commit()
        return rows

# =====================================================================
#  КЭШИРОВАНИЕ КОНФИГУРАЦИИ В ПАМЯТИ
# =====================================================================

cache_lock = threading.Lock()
RP_TIMER_SECONDS: int = int(get_config('rp_timer_seconds', '2400') or '2400')
OFFTOP_MIN_WORDS: int = int(get_config('offtop_min_words', '1') or '1')
OFFTOP_MAX_WORDS: int = int(get_config('offtop_max_words', '10') or '10')

RP_TOPICS: Set[int] = set()
OFFTOP_TOPICS: Set[int] = set()


def reload_cache():
    global RP_TIMER_SECONDS, OFFTOP_MIN_WORDS, OFFTOP_MAX_WORDS, RP_TOPICS, OFFTOP_TOPICS
    with cache_lock:
        RP_TIMER_SECONDS = int(get_config('rp_timer_seconds', '2400') or '2400')
        OFFTOP_MIN_WORDS = int(get_config('offtop_min_words', '1') or '1')
        OFFTOP_MAX_WORDS = int(get_config('offtop_max_words', '10') or '10')
        RP_TOPICS = {row[0] for row in get_topics_from_db('rp')}
        OFFTOP_TOPICS = {row[0] for row in get_topics_from_db('offtop')}


def match_topic_rule(chat_id: int, thread_id: Optional[int]) -> Tuple[Optional[str], Optional[int]]:
    with cache_lock:
        rp_s = RP_TOPICS
        off_s = OFFTOP_TOPICS

    # 1. Прямой поиск по номеру темы (thread_id)
    if thread_id is not None:
        if thread_id in rp_s:
            return 'rp', thread_id
        if thread_id in off_s:
            return 'offtop', thread_id

    # 2. Главная тема форума (General topic: thread_id=1 или None)
    if thread_id in (None, 1):
        if 1 in rp_s:
            return 'rp', 1
        if 1 in off_s:
            return 'offtop', 1

    # 3. Весь чат целиком (если в список добавлен ID группы chat_id)
    if chat_id in rp_s:
        return 'rp', chat_id
    if chat_id in off_s:
        return 'offtop', chat_id

    return None, None


def has_any_monitored_topics() -> bool:
    with cache_lock:
        return bool(RP_TOPICS or OFFTOP_TOPICS)


reload_cache()

active_timers: Dict[Tuple[int, int], threading.Timer] = {}
timers_lock = threading.Lock()

admin_states: Dict[int, Dict[str, Any]] = {}
states_lock = threading.Lock()

BOT_START_TIMESTAMP = time.time()

# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ЛОГИРОВАНИЕ
# =====================================================================

def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def format_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    rem_sec = seconds % 60
    if rem_sec == 0:
        if minutes < 60:
            return f"{minutes} мин"
        hours = minutes // 60
        rem_min = minutes % 60
        return f"{hours} ч {rem_min} мин" if rem_min else f"{hours} ч"
    return f"{minutes} мин {rem_sec} сек"


def format_uptime(seconds_total: float) -> str:
    sec = int(seconds_total)
    days = sec // 86400
    hours = (sec % 86400) // 3600
    minutes = (sec % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days} дн")
    if hours > 0:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts) if parts else "меньше минуты"


def get_current_time_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log_event(text: str):
    timestamp = get_current_time_str()
    print(f"[{timestamp}] {text}")

    if not LOG_CHAT_ID or not TOKEN:
        return

    try:
        bot.send_message(LOG_CHAT_ID, f"📋 <b>[Cleaner Log]</b> {text}", parse_mode='HTML')
    except Exception as e:
        print(f"  [LOG_ERROR] Не удалось доставить лог в чат {LOG_CHAT_ID}: {e}")

# =====================================================================
#  МЕХАНИЗМ УДАЛЕНИЯ СООБЩЕНИЙ
# =====================================================================

def delete_message_safe(chat_id: int, message_id: int) -> bool:
    try:
        bot.delete_message(chat_id, message_id)
        bump_deleted_stat(1)
        return True
    except ApiTelegramException as e:
        err_msg = str(e).lower()
        if "message to delete not found" in err_msg or "message can't be deleted" in err_msg:
            pass
        elif "too many requests" in err_msg:
            time.sleep(1.0)
        else:
            print(f"[{get_current_time_str()}] Ошибка удаления ({chat_id}:{message_id}): {e}")
        return False
    except Exception as e:
        print(f"[{get_current_time_str()}] Сетевая ошибка при удалении: {e}")
        return False
    finally:
        db_remove_pending(chat_id, message_id)
        with timers_lock:
            active_timers.pop((chat_id, message_id), None)


def schedule_deletion(chat_id: int, message_id: int, thread_id: Optional[int], delay_seconds: float,
                      user_info: str = "", snippet: str = "", persist: bool = True):
    key = (chat_id, message_id)
    delete_at = time.time() + max(0.0, delay_seconds)

    if persist:
        db_add_pending(chat_id, message_id, thread_id, delete_at, user_info, snippet)

    def _timer_callback():
        success = delete_message_safe(chat_id, message_id)
        if success:
            matched_id = thread_id if thread_id is not None else "Основная"
            log_event(
                f"🗑 <b>[РП Удаление по таймеру]</b> В теме <code>{matched_id}</code> удалено сообщение от <b>{user_info or 'Unknown'}</b> "
                f"(таймер {format_seconds(int(delay_seconds))} истёк):\n<i>«{snippet}»</i>"
            )

    with timers_lock:
        old_timer = active_timers.pop(key, None)
        if old_timer:
            old_timer.cancel()

        timer = threading.Timer(max(0.1, delay_seconds), _timer_callback)
        timer.daemon = True
        active_timers[key] = timer
        timer.start()


def cancel_scheduled_deletion(chat_id: int, message_id: int):
    key = (chat_id, message_id)
    with timers_lock:
        timer = active_timers.pop(key, None)
        if timer:
            timer.cancel()
    db_remove_pending(chat_id, message_id)


def restore_queue_on_startup():
    rows = db_all_pending()
    now = time.time()
    restored_count = 0
    fired_now_count = 0

    for chat_id, message_id, thread_id, delete_at, user_info, snippet in rows:
        remaining = delete_at - now
        if remaining <= 0:
            success = delete_message_safe(chat_id, message_id)
            if success:
                fired_now_count += 1
        else:
            schedule_deletion(chat_id, message_id, thread_id, remaining, user_info=user_info, snippet=snippet, persist=False)
            restored_count += 1

    if rows:
        log_event(
            f"🔄 <b>Восстановление очереди после рестарта:</b>\n"
            f"• Возобновлено таймеров: <code>{restored_count}</code>\n"
            f"• Просроченных удалено сразу: <code>{fired_now_count}</code>"
        )


def watchdog_background_worker():
    while True:
        try:
            time.sleep(30)
            now = time.time()
            expired = db_get_expired_pending(now)
            if expired:
                for chat_id, message_id, thread_id, user_info, snippet in expired:
                    success = delete_message_safe(chat_id, message_id)
                    if success:
                        matched_id = thread_id if thread_id is not None else "Основная"
                        log_event(
                            f"🗑 <b>[Watchdog Удаление]</b> В теме <code>{matched_id}</code> удалено просроченное сообщение от <b>{user_info or 'Unknown'}</b>:\n<i>«{snippet}»</i>"
                        )
                    time.sleep(0.05)
        except Exception as e:
            print(f"[{get_current_time_str()}] Ошибка в watchdog_worker: {e}")

# =====================================================================
#  АДМИН-ПАНЕЛЬ: ГЕНЕРАТОРЫ МЕНЮ И КЛАВИАТУР
# =====================================================================

def build_admin_main_text() -> str:
    reload_cache()
    rp_sorted = sorted(RP_TOPICS)
    offtop_sorted = sorted(OFFTOP_TOPICS)

    rp_str = ", ".join(f"<code>{tid}</code>" for tid in rp_sorted) if rp_sorted else "<i>нет тем (напишите /rp в теме)</i>"
    offtop_str = ", ".join(f"<code>{tid}</code>" for tid in offtop_sorted) if offtop_sorted else "<i>нет тем</i>"

    pending_cnt = db_count_pending()
    deleted_cnt = get_deleted_stat()
    uptime_str = format_uptime(time.time() - BOT_START_TIMESTAMP)

    text = (
        "🤖 <b>Панель управления Cleaner Bot</b>\n\n"
        f"⚡ <b>Статус:</b> <code>Активен 🟢</code> | <b>Аптайм:</b> <code>{uptime_str}</code>\n"
        f"⏱ <b>Таймер удаления RP:</b> <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n"
        f"📝 <b>Фильтр Оффтопа:</b> <code>{OFFTOP_MIN_WORDS} – {OFFTOP_MAX_WORDS} слов</code>\n\n"
        f"🗂 <b>РП-темы (удаление без # через {format_seconds(RP_TIMER_SECONDS)}):</b>\n{rp_str}\n\n"
        f"🗑 <b>Оффтоп-темы (мгновенное удаление коротких):</b>\n{offtop_str}\n\n"
        f"⏳ <b>В очереди на удаление (по таймеру):</b> <code>{pending_cnt}</code> сообщений\n"
        f"📊 <b>Всего удалено за все время:</b> <code>{deleted_cnt}</code>\n\n"
        "💡 <i>Совет: Чтобы включить автоудаление по таймеру в теме, напишите прямо в ней <code>/rp</code>!</i>"
    )
    return text


def build_admin_main_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⏱ Изменить таймер RP", callback_data="adm:timer"),
        types.InlineKeyboardButton("🧹 Очистить очередь", callback_data="adm:purge_confirm"),
    )
    kb.add(
        types.InlineKeyboardButton("➕ Добавить РП-тему", callback_data="adm:add_rp"),
        types.InlineKeyboardButton("➕ Добавить Офтоп", callback_data="adm:add_offtop"),
    )
    kb.add(
        types.InlineKeyboardButton("➖ Удалить тему", callback_data="adm:remove_menu"),
        types.InlineKeyboardButton("📋 Список тем", callback_data="adm:list_topics"),
    )
    kb.add(
        types.InlineKeyboardButton("⚙️ Лимиты оффтопа", callback_data="adm:offtop_limits"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
    )
    kb.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="adm:refresh"),
        types.InlineKeyboardButton("❌ Закрыть", callback_data="adm:close"),
    )
    return kb


def build_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="adm:cancel_input"))
    return kb


def build_remove_topics_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    all_topics = get_topics_from_db()

    for tid, ttype in all_topics:
        icon = "🗂 РП (Таймер)" if ttype == 'rp' else "🗑 Оффтоп (Мгнов.)"
        kb.add(types.InlineKeyboardButton(f"❌ {icon}: {tid}", callback_data=f"adm:del_topic:{tid}"))

    kb.add(types.InlineKeyboardButton("✍️ Ввести ID темы вручную", callback_data="adm:remove_manual"))
    kb.add(types.InlineKeyboardButton("◀️ Назад в меню", callback_data="adm:main_menu"))
    return kb

# =====================================================================
#  КОМАНДЫ АДМИНИСТРАТОРА В ЧАТАХ И ТЕМАХ
# =====================================================================

@bot.message_handler(commands=['id', 'topic', 'add_rp', 'add_offtop', 'rp', 'offtop', 'track', 'clean'])
def handle_in_chat_admin_commands(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_admin(user_id):
        return

    cmd = (message.text or "").split()[0].lower().lstrip('/')
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    effective_topic_id = thread_id if thread_id is not None else 1

    # Добавление в РП (удаление по таймеру)
    if cmd in ('add_rp', 'rp', 'track', 'clean'):
        add_topic_to_db(effective_topic_id, 'rp')
        reload_cache()
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        temp_msg = bot.send_message(
            chat_id,
            f"✅ <b>Тема ID <code>{effective_topic_id}</code> успешно привязана как РП-ТЕМА!</b>\n\n"
            f"⏱ Сообщения <b>без #</b> будут автоматически удаляться через <b>{format_seconds(RP_TIMER_SECONDS)}</b>.\n"
            f"🛡 Сообщения <b>с #</b> (например: <code>#страна</code>, <code>#ход</code>, <code>#приказ</code>) сохраняются навсегда!",
            message_thread_id=thread_id,
            parse_mode='HTML'
        )
        log_event(f"➕ <b>Админ [ID: {user_id}]</b> сделал тему <code>{effective_topic_id}</code> <b>РП-темой (Таймер {format_seconds(RP_TIMER_SECONDS)})</b>.")
        threading.Timer(8.0, lambda: delete_message_safe(chat_id, temp_msg.message_id)).start()

    # Добавление в Оффтоп (мгновенное удаление)
    elif cmd in ('add_offtop', 'offtop'):
        add_topic_to_db(effective_topic_id, 'offtop')
        reload_cache()
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        temp_msg = bot.send_message(
            chat_id,
            f"✅ <b>Тема ID <code>{effective_topic_id}</code> привязана как ОФФТОП!</b>\n\n"
            f"⚡ Короткие сообщения ({OFFTOP_MIN_WORDS}–{OFFTOP_MAX_WORDS} слов) без <code>#</code> удаляются <b>мгновенно</b>.\n"
            f"<i>(Если вы хотите удаление по таймеру {format_seconds(RP_TIMER_SECONDS)}, напишите команду <code>/rp</code>)</i>",
            message_thread_id=thread_id,
            parse_mode='HTML'
        )
        log_event(f"➕ <b>Админ [ID: {user_id}]</b> добавил тему <code>{effective_topic_id}</code> в <b>Оффтоп (Мгновенный)</b>.")
        threading.Timer(8.0, lambda: delete_message_safe(chat_id, temp_msg.message_id)).start()

    # Информация о теме
    elif cmd in ('id', 'topic'):
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(f"⏱ Сделать РП (Таймер {format_seconds(RP_TIMER_SECONDS)})", callback_data=f"adm:quick_add_rp:{effective_topic_id}"),
        )
        kb.add(
            types.InlineKeyboardButton("⚡ Сделать Оффтоп (Мгновенно)", callback_data=f"adm:quick_add_off:{effective_topic_id}"),
        )
        kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="adm:close_temp"))
        bot.send_message(
            chat_id,
            f"ℹ️ <b>Информация о ветке:</b>\n"
            f"• Чат ID: <code>{chat_id}</code>\n"
            f"• ID темы (thread_id): <code>{effective_topic_id}</code>\n\n"
            f"<i>Выберите режим для этой темы:</i>",
            message_thread_id=thread_id,
            parse_mode='HTML',
            reply_markup=kb
        )


@bot.message_handler(commands=['start', 'admin', 'menu', 'panel', 'status', 'stats', 'purge'])
def handle_admin_commands(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_admin(user_id):
        return

    with states_lock:
        admin_states.pop(user_id, None)

    bot.send_message(
        message.chat.id,
        build_admin_main_text(),
        parse_mode='HTML',
        reply_markup=build_admin_main_keyboard()
    )


@bot.message_handler(func=lambda m: is_admin(m.from_user.id if m.from_user else 0) and (m.from_user.id in admin_states), content_types=['text'])
def handle_admin_text_input(message: types.Message):
    user_id = message.from_user.id
    with states_lock:
        state = admin_states.pop(user_id, None)

    if not state:
        return

    action = state.get('action')
    text = (message.text or "").strip()

    if text.lower() in ('/cancel', 'отмена', 'cancel'):
        bot.send_message(message.chat.id, "❌ Действие отменено.", reply_markup=build_admin_main_keyboard())
        return

    # 1. Изменение таймера RP
    if action == 'set_timer':
        try:
            val_str = text.replace(',', '.')
            minutes = float(val_str)
            if minutes <= 0:
                raise ValueError
            seconds = int(minutes * 60)
            if seconds < 5:
                bot.send_message(message.chat.id, "❌ Минимальный таймер: 5 секунд. Введите число минут:")
                with states_lock:
                    admin_states[user_id] = {'action': 'set_timer'}
                return

            set_config('rp_timer_seconds', seconds)
            reload_cache()

            msg = f"✅ <b>Таймер RP-тем успешно изменен на {format_seconds(seconds)}!</b> ({minutes:g} мин)"
            bot.send_message(message.chat.id, msg, parse_mode='HTML', reply_markup=build_admin_main_keyboard())
            log_event(f"⏱ <b>Админ [ID: {user_id}]</b> изменил таймер RP-тем на <b>{format_seconds(seconds)}</b>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ Некорректное число минут! Введите положительное число (например: <code>10</code> или <code>40</code>):",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'set_timer'}

    # 2. Добавление RP-темы
    elif action == 'add_rp':
        try:
            topic_id = int(text)
            add_topic_to_db(topic_id, 'rp')
            reload_cache()

            bot.send_message(
                message.chat.id,
                f"✅ <b>Тема {topic_id} успешно добавлена в список РП!</b>\n"
                f"Сообщения без <code>#</code> в ней будут удаляться через <b>{format_seconds(RP_TIMER_SECONDS)}</b>.",
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
            log_event(f"➕ <b>Админ [ID: {user_id}]</b> добавил тему <code>{topic_id}</code> в <b>РП (Таймер {format_seconds(RP_TIMER_SECONDS)})</b>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ ID темы должен быть целым числом! Попробуйте еще раз:",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'add_rp'}

    # 3. Добавление Оффтоп-темы
    elif action == 'add_offtop':
        try:
            topic_id = int(text)
            add_topic_to_db(topic_id, 'offtop')
            reload_cache()

            bot.send_message(
                message.chat.id,
                f"✅ <b>Тема {topic_id} успешно добавлена в список ОФФТОП (мгновенное удаление)!</b>",
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
            log_event(f"➕ <b>Админ [ID: {user_id}]</b> добавил тему <code>{topic_id}</code> в <b>Оффтоп</b>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ ID темы должен быть целым числом! Попробуйте еще раз:",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'add_offtop'}

    # 4. Удаление темы вручную
    elif action == 'remove_manual':
        try:
            topic_id = int(text)
            remove_topic_from_db(topic_id)
            reload_cache()

            bot.send_message(
                message.chat.id,
                f"✅ <b>Тема {topic_id} удалена из списков отслеживания.</b>",
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
            log_event(f"➖ <b>Админ [ID: {user_id}]</b> удалил тему <code>{topic_id}</code>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ ID темы должен быть целым числом! Попробуйте еще раз:",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'remove_manual'}

    # 5. Лимиты оффтопа
    elif action == 'set_offtop_limits':
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            min_w = int(parts[0])
            max_w = int(parts[1])
            if min_w <= max_w and min_w >= 0:
                set_config('offtop_min_words', min_w)
                set_config('offtop_max_words', max_w)
                reload_cache()

                bot.send_message(
                    message.chat.id,
                    f"✅ <b>Лимиты оффтопа успешно обновлены: от {min_w} до {max_w} слов!</b>",
                    parse_mode='HTML',
                    reply_markup=build_admin_main_keyboard()
                )
                log_event(f"⚙️ <b>Админ [ID: {user_id}]</b> изменил фильтр оффтопа: <code>{min_w}-{max_w}</code> слов.")
                return

        bot.send_message(
            message.chat.id,
            "❌ Неверный формат! Введите два числа через пробел (например: <code>1 10</code>):",
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )
        with states_lock:
            admin_states[user_id] = {'action': 'set_offtop_limits'}

# =====================================================================
#  ОБРАБОТЧИКИ ИНЛАЙН-КНОПОК (CALLBACK QUERY)
# =====================================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call: types.CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if not is_admin(user_id):
        bot.answer_callback_query(call.id)
        return

    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # 1. Быстрое добавление темы через кнопку в группе
    if data.startswith("adm:quick_add_rp:"):
        tid = int(data.split(":")[-1])
        add_topic_to_db(tid, 'rp')
        reload_cache()
        bot.answer_callback_query(call.id, f"✅ Тема {tid} привязана к РП (Таймер {format_seconds(RP_TIMER_SECONDS)})!")
        try:
            bot.edit_message_text(f"✅ <b>Тема ID {tid} привязана как РП-ТЕМА!</b>\nУдаление сообщений без # через {format_seconds(RP_TIMER_SECONDS)}.", chat_id=chat_id, message_id=msg_id, parse_mode='HTML')
            threading.Timer(5.0, lambda: delete_message_safe(chat_id, msg_id)).start()
        except Exception:
            pass
        log_event(f"➕ <b>Админ [ID: {user_id}]</b> привязал тему <code>{tid}</code> как <b>РП (Таймер {format_seconds(RP_TIMER_SECONDS)})</b>.")
        return

    elif data.startswith("adm:quick_add_off:"):
        tid = int(data.split(":")[-1])
        add_topic_to_db(tid, 'offtop')
        reload_cache()
        bot.answer_callback_query(call.id, f"✅ Тема {tid} привязана к Оффтопу (Мгновенно)!")
        try:
            bot.edit_message_text(f"✅ <b>Тема ID {tid} привязана как ОФФТОП (Мгновенное удаление)!</b>", chat_id=chat_id, message_id=msg_id, parse_mode='HTML')
            threading.Timer(5.0, lambda: delete_message_safe(chat_id, msg_id)).start()
        except Exception:
            pass
        log_event(f"➕ <b>Админ [ID: {user_id}]</b> привязал тему <code>{tid}</code> как <b>Оффтоп</b>.")
        return

    elif data == "adm:close_temp":
        bot.answer_callback_query(call.id)
        delete_message_safe(chat_id, msg_id)
        return

    # 2. Главное меню / Назад / Обновить
    if data in ("adm:main_menu", "adm:refresh"):
        with states_lock:
            admin_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "🔄 Обновлено")
        try:
            bot.edit_message_text(
                build_admin_main_text(),
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
        except Exception:
            pass

    # 3. Закрыть меню
    elif data == "adm:close":
        with states_lock:
            admin_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "Панель закрыта")
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    # 4. Отмена текущего ввода
    elif data == "adm:cancel_input":
        with states_lock:
            admin_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "Отменено")
        try:
            bot.edit_message_text(
                build_admin_main_text(),
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
        except Exception:
            bot.send_message(chat_id, build_admin_main_text(), parse_mode='HTML', reply_markup=build_admin_main_keyboard())

    # 5. Запрос на изменение таймера
    elif data == "adm:timer":
        with states_lock:
            admin_states[user_id] = {'action': 'set_timer'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏱ <b>Настройка таймера RP-тем</b>\n\n"
            f"Текущее значение: <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n\n"
            f"Пришлите новое время в <b>минутах</b> (например: <code>10</code> или <code>40</code>):",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 6. Запрос на добавление RP темы
    elif data == "adm:add_rp":
        with states_lock:
            admin_states[user_id] = {'action': 'add_rp'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"➕ <b>Добавление RP-темы (Удаление по таймеру)</b>\n\n"
            f"Пришлите <b>ID темы (thread_id)</b> форума:\n"
            f"<i>(Сообщения без # в ней будут удаляться через {format_seconds(RP_TIMER_SECONDS)})</i>\n\n"
            f"💡 <i>Или просто напишите команду <code>/rp</code> прямо внутри нужной темы в группе!</i>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 7. Запрос на добавление Оффтоп темы
    elif data == "adm:add_offtop":
        with states_lock:
            admin_states[user_id] = {'action': 'add_offtop'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"➕ <b>Добавление Оффтоп-темы (Мгновенное удаление)</b>\n\n"
            f"Пришлите <b>ID темы (thread_id)</b> форума:\n"
            f"<i>(Короткие сообщения от {OFFTOP_MIN_WORDS} до {OFFTOP_MAX_WORDS} слов без # будут удаляться сразу)</i>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 8. Меню удаления тем
    elif data == "adm:remove_menu":
        bot.answer_callback_query(call.id)
        all_topics = get_topics_from_db()
        if not all_topics:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:main_menu"))
            bot.edit_message_text(
                "ℹ️ <b>Список тем пуст!</b> Сначала добавьте тему (напишите <code>/rp</code> в нужной теме группы).",
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode='HTML',
                reply_markup=kb
            )
            return

        bot.edit_message_text(
            "➖ <b>Выберите тему для удаления из отслеживаемых:</b>\n"
            "<i>(Нажмите на кнопку с темой для моментального удаления)</i>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_remove_topics_keyboard()
        )

    # 9. Удаление темы по кнопке
    elif data.startswith("adm:del_topic:"):
        topic_id_str = data.split(":")[-1]
        try:
            topic_id = int(topic_id_str)
            remove_topic_from_db(topic_id)
            reload_cache()
            bot.answer_callback_query(call.id, f"✅ Тема {topic_id} удалена!")
            log_event(f"➖ <b>Админ [ID: {user_id}]</b> удалил тему <code>{topic_id}</code>.")

            all_topics = get_topics_from_db()
            if all_topics:
                bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=build_remove_topics_keyboard())
            else:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("◀️ В главное меню", callback_data="adm:main_menu"))
                bot.edit_message_text("✅ Все темы удалены. Список пуст.", chat_id=chat_id, message_id=msg_id, reply_markup=kb)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка: {e}")

    # 10. Удаление темы ручным вводом
    elif data == "adm:remove_manual":
        with states_lock:
            admin_states[user_id] = {'action': 'remove_manual'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "➖ Пришлите <b>ID темы (число)</b>, которую нужно удалить:",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 11. Список тем
    elif data == "adm:list_topics":
        bot.answer_callback_query(call.id)
        reload_cache()
        rp_list = "\n".join(f"• <code>{tid}</code>" for tid in sorted(RP_TOPICS)) or "<i>(нет РП-тем)</i>"
        offtop_list = "\n".join(f"• <code>{tid}</code>" for tid in sorted(OFFTOP_TOPICS)) or "<i>(нет оффтоп-тем)</i>"

        text = (
            "📋 <b>Список отслеживаемых тем:</b>\n\n"
            f"🗂 <b>РП-темы (удаление без # через {format_seconds(RP_TIMER_SECONDS)}):</b>\n{rp_list}\n\n"
            f"🗑 <b>Оффтоп-темы (мгновенно, {OFFTOP_MIN_WORDS}-{OFFTOP_MAX_WORDS} слов):</b>\n{offtop_list}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Добавить РП", callback_data="adm:add_rp"),
            types.InlineKeyboardButton("➕ Добавить Оффтоп", callback_data="adm:add_offtop"),
        )
        kb.add(types.InlineKeyboardButton("◀️ В главное меню", callback_data="adm:main_menu"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', reply_markup=kb)

    # 12. Лимиты оффтопа
    elif data == "adm:offtop_limits":
        with states_lock:
            admin_states[user_id] = {'action': 'set_offtop_limits'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⚙️ <b>Настройка лимитов слов для Оффтопа</b>\n\n"
            f"Текущие лимиты: от <b>{OFFTOP_MIN_WORDS}</b> до <b>{OFFTOP_MAX_WORDS}</b> слов.\n\n"
            f"Пришлите два числа через пробел (минимум и максимум), например: <code>1 10</code>:",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 13. Детальная статистика
    elif data == "adm:stats":
        bot.answer_callback_query(call.id)
        reload_cache()
        with timers_lock:
            active_ram_timers = len(active_timers)
        pending_db = db_count_pending()
        uptime_str = format_uptime(time.time() - BOT_START_TIMESTAMP)

        text = (
            "📊 <b>Детальная статистика Cleaner Bot:</b>\n\n"
            f"🕒 <b>Аптайм бота:</b> <code>{uptime_str}</code>\n"
            f"🗑 <b>Всего сообщений удалено:</b> <code>{get_deleted_stat()}</code>\n"
            f"⏳ <b>В очереди на удаление (по таймеру):</b> <code>{pending_db}</code>\n"
            f"⚡ <b>Активных таймеров в RAM:</b> <code>{active_ram_timers}</code>\n"
            f"🗂 <b>Количество РП-тем (с таймером):</b> <code>{len(RP_TOPICS)}</code>\n"
            f"🗑 <b>Количество Оффтоп-тем (мгновенных):</b> <code>{len(OFFTOP_TOPICS)}</code>\n"
            f"⏱ <b>Текущий таймер RP:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>\n"
            f"📝 <b>Фильтр Оффтопа:</b> <code>{OFFTOP_MIN_WORDS} - {OFFTOP_MAX_WORDS} слов</code>"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("🔄 Обновить", callback_data="adm:stats"),
            types.InlineKeyboardButton("◀️ В меню", callback_data="adm:main_menu")
        )
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', reply_markup=kb)
        except Exception:
            pass

    # 14. Подтверждение очистки очереди
    elif data == "adm:purge_confirm":
        pending_count = db_count_pending()
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("🧹 ДА, УДАЛИТЬ ВСЁ СЕЙЧАС", callback_data="adm:purge_exec"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="adm:main_menu")
        )
        bot.edit_message_text(
            f"⚠️ <b>Подтверждение экстренной очистки очереди</b>\n\n"
            f"В очереди прямо сейчас: <b>{pending_count}</b> сообщений.\n"
            f"Вы действительно хотите принудительно удалить их прямо сейчас из чатов?",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=kb
        )

    # 15. Выполнение очистки очереди
    elif data == "adm:purge_exec":
        bot.answer_callback_query(call.id, "🧹 Запуск очистки...")

        with timers_lock:
            for timer in active_timers.values():
                timer.cancel()
            active_timers.clear()

        rows = db_clear_all_pending()
        deleted_count = 0
        failed_count = 0

        for chat_to_del, msg_to_del in rows:
            try:
                bot.delete_message(chat_to_del, msg_to_del)
                deleted_count += 1
            except Exception:
                failed_count += 1
            time.sleep(0.04)

        bump_deleted_stat(deleted_count)

        bot.edit_message_text(
            f"✅ <b>Очистка очереди завершена!</b>\n\n"
            f"• Успешно удалено сообщений: <b>{deleted_count}</b>\n"
            f"• Уже удалены ранее / ошибок: <b>{failed_count}</b>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_admin_main_keyboard()
        )
        log_event(
            f"🧹 <b>Админ [ID: {user_id}]</b> выполнил ручную очистку очереди:\n"
            f"Удалено сообщений: <b>{deleted_count}</b> (ошибок: {failed_count})."
        )

# =====================================================================
#  ОБРАБОТЧИКИ ЧИСТКИ СООБЩЕНИЙ В ГРУППАХ И ТЕМАХ
# =====================================================================

@bot.message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and has_any_monitored_topics(),
    content_types=CONTENT_TYPES
)
def handle_group_message(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id

    # Определяем правило для этой темы: 'rp' (по таймеру) или 'offtop' (мгновенно)
    rule_type, matched_id = match_topic_rule(chat_id, thread_id)
    if not rule_type:
        return  # Тема не отслеживается ботом

    text = message.text or message.caption or ""

    # ПРАВИЛО: Сообщения с хэштегом # (например #страна, #ход, #приказ) НЕ УДАЛЯЮТСЯ!
    if '#' in text:
        return

    user_name = f"@{message.from_user.username}" if message.from_user and message.from_user.username else f"ID: {message.from_user.id if message.from_user else 'Unknown'}"
    snippet = (text[:60] + "...") if len(text) > 60 else (text or "[Медиа/Стикер]")

    # 1. ПРАВИЛО РП-ТЕМЫ (УДАЛЕНИЕ ВСЕХ СООБЩЕНИЙ БЕЗ # ЧЕРЕЗ ТАЙМЕР 40 МИНУТ)
    if rule_type == 'rp':
        schedule_deletion(
            chat_id=chat_id,
            message_id=message.message_id,
            thread_id=thread_id,
            delay_seconds=RP_TIMER_SECONDS,
            user_info=user_name,
            snippet=snippet,
            persist=True
        )

    # 2. ПРАВИЛО ОФФТОП-ТЕМЫ (МГНОВЕННОЕ УДАЛЕНИЕ КОРОТКИХ СООБЩЕНИЙ)
    elif rule_type == 'offtop':
        word_count = len(text.split()) if text else 0
        if OFFTOP_MIN_WORDS <= word_count <= OFFTOP_MAX_WORDS:
            success = delete_message_safe(chat_id, message.message_id)
            if success:
                log_event(
                    f"🗑 <b>[Оффтоп Мгновенно]</b> В теме <code>{matched_id}</code> удалено сообщение от <b>{user_name}</b> "
                    f"(слов: {word_count}):\n<i>«{snippet}»</i>"
                )


@bot.edited_message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and has_any_monitored_topics(),
    content_types=CONTENT_TYPES
)
def handle_group_edited_message(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id

    rule_type, matched_id = match_topic_rule(chat_id, thread_id)
    if not rule_type:
        return

    text = message.text or message.caption or ""
    user_name = f"@{message.from_user.username}" if message.from_user and message.from_user.username else f"ID: {message.from_user.id if message.from_user else 'Unknown'}"
    snippet = (text[:60] + "...") if len(text) > 60 else (text or "[Медиа/Стикер]")

    # Если пользователь отредактировал сообщение и ДОБАВИЛ хэштег #
    if '#' in text:
        cancel_scheduled_deletion(chat_id, message.message_id)
        if rule_type == 'rp':
            log_event(f"🛡 <b>[РП Сохранено]</b> Сообщение {message.message_id} в теме <code>{matched_id}</code> от <b>{user_name}</b> сохранено (добавлен хэштег #).")

    # Если пользователь отредактировал сообщение и УБРАЛ хэштег #
    else:
        if rule_type == 'rp':
            key = (chat_id, message.message_id)
            with timers_lock:
                already_scheduled = key in active_timers
            if not already_scheduled:
                schedule_deletion(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    thread_id=thread_id,
                    delay_seconds=RP_TIMER_SECONDS,
                    user_info=user_name,
                    snippet=snippet,
                    persist=True
                )
                log_event(f"⏱ <b>[РП Таймер]</b> Запущен таймер удаления для сообщения {message.message_id} в теме <code>{matched_id}</code> от <b>{user_name}</b> (убран хэштег #).")

        elif rule_type == 'offtop':
            word_count = len(text.split()) if text else 0
            if OFFTOP_MIN_WORDS <= word_count <= OFFTOP_MAX_WORDS:
                delete_message_safe(chat_id, message.message_id)

# =====================================================================
#  ТОЧКА ВХОДА И ЦИКЛ ЗАПУСКА С АВТОВОССТАНОВЛЕНИЕМ
# =====================================================================

def main():
    if not TOKEN:
        print("[CRITICAL] Переменная BOT_TOKEN не задана на хостинге!")
        sys.exit(1)

    print(f"[{get_current_time_str()}] Запуск Telegram Cleaner Bot...")
    print(f"  • Админы: {sorted(ADMIN_IDS)}")
    print(f"  • Лог-чат: {LOG_CHAT_ID}")
    print(f"  • База данных: {DB_PATH}")
    print(f"  • РП-темы (с таймером): {sorted(RP_TOPICS) or 'нет'}")
    print(f"  • Оффтоп-темы (мгновенные): {sorted(OFFTOP_TOPICS) or 'нет'}")
    print(f"  • Таймер RP: {format_seconds(RP_TIMER_SECONDS)}")

    start_health_check_server()
    restore_queue_on_startup()

    watchdog_thread = threading.Thread(target=watchdog_background_worker, daemon=True, name="WatchdogWorker")
    watchdog_thread.start()

    log_event(
        f"🚀 <b>Cleaner Bot успешно запущен!</b>\n\n"
        f"⏱ <b>Таймер RP:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>\n"
        f"🗂 <b>РП-темы (удаление без # по таймеру):</b> <code>{sorted(RP_TOPICS) or '—'}</code>\n"
        f"🗑 <b>Оффтоп-темы (мгновенно):</b> <code>{sorted(OFFTOP_TOPICS) or '—'}</code>"
    )

    RETRY_DELAYS = [3, 5, 10, 20, 30]
    attempt = 0

    while True:
        try:
            attempt = 0
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=40,
                skip_pending=False,
                logger_level=None
            )
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as net_err:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(f"[{get_current_time_str()}] Потеря соединения ({type(net_err).__name__}). Переподключение через {delay}с...")
            time.sleep(delay)
            attempt += 1
        except KeyboardInterrupt:
            print(f"[{get_current_time_str()}] Бот остановлен вручную.")
            break
        except Exception as e:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(f"[{get_current_time_str()}] Критическая ошибка в polling: {e}. Перезапуск через {delay}с...")
            log_event(f"❌ <b>Критическая ошибка в Polling:</b> <code>{e}</code>. Перезапуск через {delay}с...")
            time.sleep(delay)
            attempt += 1


if __name__ == '__main__':
    main()
