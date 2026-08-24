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

# ID главного администратора (только ты имеешь доступ к админке и командам)
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
ENV_TOPICS_RAW = os.getenv('RP_TOPICS', '') or os.getenv('OFFTOP_TOPICS', '') or os.getenv('TOPICS', '')

# Все типы контента (текст, стикеры, гифки, фото, видео, кружочки, голосовые и т.д.)
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
    """Создает таблицы базы данных при старте, если они не существуют."""
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
            topic_id INTEGER PRIMARY KEY
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
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('timer_seconds', '2400');")  # 40 минут по умолчанию

        # Если в ENV заданы дефолтные темы, добавляем их
        if ENV_TOPICS_RAW:
            for part in ENV_TOPICS_RAW.replace(',', ' ').replace(';', ' ').split():
                if part.lstrip('-').isdigit():
                    conn.execute("INSERT OR IGNORE INTO topics (topic_id) VALUES (?)", (int(part),))

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


def get_topics_from_db() -> List[int]:
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT topic_id FROM topics ORDER BY topic_id ASC").fetchall()
        return [r[0] for r in rows]


def add_topic_to_db(topic_id: int):
    with db_lock, db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO topics (topic_id) VALUES (?)", (int(topic_id),))
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


def db_is_pending(chat_id: int, message_id: int) -> bool:
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT 1 FROM pending_deletions WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        return bool(row)


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
RP_TIMER_SECONDS: int = int(get_config('timer_seconds', '2400') or '2400')
TRACKED_TOPICS: Set[int] = set()


def reload_cache():
    """Синхронизирует переменные в оперативной памяти с базой SQLite."""
    global RP_TIMER_SECONDS, TRACKED_TOPICS
    with cache_lock:
        RP_TIMER_SECONDS = int(get_config('timer_seconds', '2400') or '2400')
        TRACKED_TOPICS = set(get_topics_from_db())


def is_topic_monitored(chat_id: int, thread_id: Optional[int]) -> bool:
    """Проверяет, включена ли очистка в данной теме форума или группе."""
    with cache_lock:
        s = TRACKED_TOPICS

    # 1. По номеру темы (thread_id)
    if thread_id is not None and thread_id in s:
        return True

    # 2. По Главной теме (General topic: thread_id == 1 или None)
    if thread_id in (None, 1) and 1 in s:
        return True

    # 3. По ID всего чата
    if chat_id in s:
        return True

    return False


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
        # СТРОГАЯ ПРОВЕРКА: если сообщение отменили (добавили #), его НЕТ в БД — не удаляем!
        if not db_is_pending(chat_id, message_id):
            return

        success = delete_message_safe(chat_id, message_id)
        if success:
            matched_id = thread_id if thread_id is not None else "Основная"
            log_event(
                f"🗑 <b>[Удаление по таймеру]</b> В теме <code>{matched_id}</code> удалено сообщение от <b>{user_info or 'Unknown'}</b> "
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
    """
    Отменяет запланированное удаление (например, если в сообщение добавили #).
    Полностью удаляет запись из базы и гасит поток таймера.
    """
    key = (chat_id, message_id)
    with timers_lock:
        timer = active_timers.pop(key, None)
        if timer:
            timer.cancel()
    db_remove_pending(chat_id, message_id)


def restore_queue_on_startup():
    """Восстанавливает отложенные удаления после рестарта бота."""
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
    """Фоновый сторож: проверяет очередь каждые 30 секунд."""
    while True:
        try:
            time.sleep(30)
            now = time.time()
            expired = db_get_expired_pending(now)
            if expired:
                for chat_id, message_id, thread_id, user_info, snippet in expired:
                    if not db_is_pending(chat_id, message_id):
                        continue
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
    topics_sorted = sorted(TRACKED_TOPICS)
    topics_str = ", ".join(f"<code>{tid}</code>" for tid in topics_sorted) if topics_sorted else "<i>нет тем (напишите /offtop в нужной теме)</i>"

    pending_cnt = db_count_pending()
    deleted_cnt = get_deleted_stat()
    uptime_str = format_uptime(time.time() - BOT_START_TIMESTAMP)

    text = (
        "🤖 <b>Панель управления Cleaner Bot (Райс)</b>\n\n"
        f"⚡ <b>Статус:</b> <code>Активен 🟢</code> | <b>Аптайм:</b> <code>{uptime_str}</code>\n"
        f"⏱ <b>Таймер удаления:</b> <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n\n"
        f"🗂 <b>Отслеживаемые темы:</b>\n{topics_str}\n\n"
        f"⏳ <b>Сообщений в очереди:</b> <code>{pending_cnt}</code>\n"
        f"📊 <b>Всего удалено за все время:</b> <code>{deleted_cnt}</code>\n\n"
        "💡 <i>Правило: Все сообщения, гифки и стикеры без <code>#</code> удаляются через таймер. Сообщения с <code>#</code> сохраняются навсегда!</i>"
    )
    return text


def build_admin_main_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⏱ Изменить таймер", callback_data="adm:timer"),
        types.InlineKeyboardButton("🧹 Очистить очередь", callback_data="adm:purge_confirm"),
    )
    kb.add(
        types.InlineKeyboardButton("➕ Добавить тему", callback_data="adm:add_topic"),
        types.InlineKeyboardButton("➖ Удалить тему", callback_data="adm:remove_menu"),
    )
    kb.add(
        types.InlineKeyboardButton("📋 Список тем", callback_data="adm:list_topics"),
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

    for tid in all_topics:
        kb.add(types.InlineKeyboardButton(f"❌ Тема: {tid}", callback_data=f"adm:del_topic:{tid}"))

    kb.add(types.InlineKeyboardButton("✍️ Ввести ID темы вручную", callback_data="adm:remove_manual"))
    kb.add(types.InlineKeyboardButton("◀️ Назад в меню", callback_data="adm:main_menu"))
    return kb

# =====================================================================
#  КОМАНДЫ АДМИНИСТРАТОРА В ЧАТАХ И ТЕМАХ
# =====================================================================

@bot.message_handler(commands=['offtop', 'rp', 'clean', 'track', 'add_topic'])
def handle_in_chat_offtop_command(message: types.Message):
    """
    При отправке команды /offtop в тему:
    - Бот привязывает тему для отслеживания и очистки по таймеру
    - Отвечает "скор индюк"
    - Логирует действие
    """
    user_id = message.from_user.id if message.from_user else 0
    if not is_admin(user_id):
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    effective_topic_id = thread_id if thread_id is not None else 1

    add_topic_to_db(effective_topic_id)
    reload_cache()

    # Отвечаем "скор индюк", как и просили
    bot.reply_to(message, "скор индюк")
    log_event(f"➕ <b>Админ [ID: {user_id}]</b> включил очистку в теме <code>{effective_topic_id}</code> командой <code>/offtop</code> (скор индюк).")


@bot.message_handler(commands=['id', 'topic'])
def handle_in_chat_id_command(message: types.Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_admin(user_id):
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    effective_topic_id = thread_id if thread_id is not None else 1

    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Включить очистку в этой теме", callback_data=f"adm:quick_add:{effective_topic_id}"))
    kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="adm:close_temp"))

    bot.send_message(
        chat_id,
        f"ℹ️ <b>Информация о ветке:</b>\n"
        f"• Чат ID: <code>{chat_id}</code>\n"
        f"• ID темы (thread_id): <code>{effective_topic_id}</code>\n"
        f"• Таймер удаления: <b>{format_seconds(RP_TIMER_SECONDS)}</b>",
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

    # 1. Изменение таймера
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

            set_config('timer_seconds', seconds)
            reload_cache()

            msg = f"✅ <b>Таймер удаления успешно изменен на {format_seconds(seconds)}!</b> ({minutes:g} мин)"
            bot.send_message(message.chat.id, msg, parse_mode='HTML', reply_markup=build_admin_main_keyboard())
            log_event(f"⏱ <b>Админ [ID: {user_id}]</b> изменил таймер на <b>{format_seconds(seconds)}</b>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ Некорректное число минут! Введите число минут (например: <code>10</code> или <code>40</code>):",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'set_timer'}

    # 2. Добавление темы
    elif action == 'add_topic':
        try:
            topic_id = int(text)
            add_topic_to_db(topic_id)
            reload_cache()

            bot.send_message(
                message.chat.id,
                f"✅ <b>Тема {topic_id} успешно добавлена в список отслеживания!</b>\n"
                f"Сообщения, гифки и стикеры без <code>#</code> будут удаляться через <b>{format_seconds(RP_TIMER_SECONDS)}</b>.",
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
            log_event(f"➕ <b>Админ [ID: {user_id}]</b> добавил тему <code>{topic_id}</code>.")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ ID темы должен быть целым числом! Попробуйте еще раз:",
                parse_mode='HTML',
                reply_markup=build_cancel_keyboard()
            )
            with states_lock:
                admin_states[user_id] = {'action': 'add_topic'}

    # 3. Удаление темы вручную
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
    if data.startswith("adm:quick_add:"):
        tid = int(data.split(":")[-1])
        add_topic_to_db(tid)
        reload_cache()
        bot.answer_callback_query(call.id, f"✅ Очистка включена в теме {tid}!")
        try:
            bot.edit_message_text(f"✅ <b>Тема ID {tid} добавлена!</b>\nУдаление без # через {format_seconds(RP_TIMER_SECONDS)}.", chat_id=chat_id, message_id=msg_id, parse_mode='HTML')
            threading.Timer(5.0, lambda: delete_message_safe(chat_id, msg_id)).start()
        except Exception:
            pass
        log_event(f"➕ <b>Админ [ID: {user_id}]</b> включил тему <code>{tid}</code> (Таймер {format_seconds(RP_TIMER_SECONDS)}).")
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

    # 5. Изменение таймера
    elif data == "adm:timer":
        with states_lock:
            admin_states[user_id] = {'action': 'set_timer'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏱ <b>Настройка таймера удаления сообщений</b>\n\n"
            f"Текущее значение: <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n\n"
            f"Пришлите новое время в <b>минутах</b> (например: <code>10</code> или <code>40</code>):",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 6. Добавление темы
    elif data == "adm:add_topic":
        with states_lock:
            admin_states[user_id] = {'action': 'add_topic'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"➕ <b>Добавление темы для очистки</b>\n\n"
            f"Пришлите <b>ID темы (thread_id)</b> форума:\n"
            f"<i>(Сообщения, гифки и стикеры без # в ней будут удаляться через {format_seconds(RP_TIMER_SECONDS)})</i>\n\n"
            f"💡 <i>Или просто напишите команду <code>/offtop</code> прямо внутри нужной темы в группе!</i>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 7. Меню удаления тем
    elif data == "adm:remove_menu":
        bot.answer_callback_query(call.id)
        all_topics = get_topics_from_db()
        if not all_topics:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("◀️ Назад", callback_data="adm:main_menu"))
            bot.edit_message_text(
                "ℹ️ <b>Список тем пуст!</b> Сначала добавьте тему (напишите <code>/offtop</code> в нужной теме группы).",
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

    # 8. Удаление темы по кнопке
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

    # 9. Удаление темы ручным вводом
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

    # 10. Список тем
    elif data == "adm:list_topics":
        bot.answer_callback_query(call.id)
        reload_cache()
        topics_list = "\n".join(f"• <code>{tid}</code>" for tid in sorted(TRACKED_TOPICS)) or "<i>(нет тем)</i>"

        text = (
            "📋 <b>Список отслеживаемых тем:</b>\n\n"
            f"🗂 <b>Таймер удаления без #: {format_seconds(RP_TIMER_SECONDS)}</b>\n\n"
            f"{topics_list}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Добавить тему", callback_data="adm:add_topic"),
            types.InlineKeyboardButton("➖ Удалить тему", callback_data="adm:remove_menu"),
        )
        kb.add(types.InlineKeyboardButton("◀️ В главное меню", callback_data="adm:main_menu"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', reply_markup=kb)

    # 11. Детальная статистика
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
            f"🗂 <b>Количество тем:</b> <code>{len(TRACKED_TOPICS)}</code>\n"
            f"⏱ <b>Текущий таймер:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>"
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

    # 12. Подтверждение очистки очереди
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

    # 13. Выполнение очистки очереди
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
    func=lambda m: m.chat.type in ('group', 'supergroup'),
    content_types=CONTENT_TYPES
)
def handle_group_message(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id

    # Проверяем, отслеживается ли эта тема
    if not is_topic_monitored(chat_id, thread_id):
        return

    text = message.text or message.caption or ""

    # ПРАВИЛО: Сообщения с хэштегом # (например #страна, #ход, #приказ, #газета) НЕ УДАЛЯЮТСЯ!
    if '#' in text:
        return

    # Сообщения БЕЗ # (включая текст, стикеры, гифки, фото, видео) планируются на удаление по таймеру!
    user_name = f"@{message.from_user.username}" if message.from_user and message.from_user.username else f"ID: {message.from_user.id if message.from_user else 'Unknown'}"
    snippet = (text[:60] + "...") if len(text) > 60 else (text or f"[{message.content_type.upper()}]")

    schedule_deletion(
        chat_id=chat_id,
        message_id=message.message_id,
        thread_id=thread_id,
        delay_seconds=RP_TIMER_SECONDS,
        user_info=user_name,
        snippet=snippet,
        persist=True
    )


@bot.edited_message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup'),
    content_types=CONTENT_TYPES
)
def handle_group_edited_message(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id

    text = message.text or message.caption or ""
    user_name = f"@{message.from_user.username}" if message.from_user and message.from_user.username else f"ID: {message.from_user.id if message.from_user else 'Unknown'}"
    snippet = (text[:60] + "...") if len(text) > 60 else (text or f"[{message.content_type.upper()}]")

    # Если пользователь отредактировал сообщение и ДОБАВИЛ хэштег #
    if '#' in text:
        # Моментально гасим таймер и удаляем из БД
        cancel_scheduled_deletion(chat_id, message.message_id)
        matched_id = thread_id if thread_id is not None else "Основная"
        log_event(f"🛡 <b>[Сообщение сохранено]</b> Сообщение {message.message_id} в теме <code>{matched_id}</code> от <b>{user_name}</b> сохранено (добавлен хэштег #).")

    # Если пользователь отредактировал сообщение и УБРАЛ хэштег #
    else:
        if is_topic_monitored(chat_id, thread_id):
            if not db_is_pending(chat_id, message.message_id):
                schedule_deletion(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    thread_id=thread_id,
                    delay_seconds=RP_TIMER_SECONDS,
                    user_info=user_name,
                    snippet=snippet,
                    persist=True
                )
                matched_id = thread_id if thread_id is not None else "Основная"
                log_event(f"⏱ <b>[Таймер запущен]</b> Запущен таймер удаления для сообщения {message.message_id} в теме <code>{matched_id}</code> от <b>{user_name}</b> (убран хэштег #).")

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
    print(f"  • Темы очистки: {sorted(TRACKED_TOPICS) or 'нет'}")
    print(f"  • Таймер удаления: {format_seconds(RP_TIMER_SECONDS)}")

    start_health_check_server()
    restore_queue_on_startup()

    watchdog_thread = threading.Thread(target=watchdog_background_worker, daemon=True, name="WatchdogWorker")
    watchdog_thread.start()

    log_event(
        f"🚀 <b>Cleaner Bot успешно запущен!</b>\n\n"
        f"⏱ <b>Таймер удаления:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>\n"
        f"🗂 <b>Отслеживаемые темы:</b> <code>{sorted(TRACKED_TOPICS) or '—'}</code>"
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
