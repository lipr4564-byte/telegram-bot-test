#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import sqlite3
import threading
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

# ID главного администратора и список админов.
# Доступ к админ-панели и командам есть ТОЛЬКО у этих ID.
# Обычные пользователи полностью игнорируются (бот им не отвечает).
ADMIN_IDS: Set[int] = {7930482209}

env_admin = os.getenv('ADMIN_ID', '').strip()
if env_admin:
    for part in env_admin.replace(',', ' ').split():
        if part.isdigit():
            ADMIN_IDS.add(int(part))

# Группа для отправки системных логов
LOG_CHAT_ID: int = int(os.getenv('LOG_CHAT_ID', '-5288630698'))

# Путь к файлу базы данных SQLite
DB_PATH: str = os.getenv('DB_PATH', 'bot_data.db')

# Типы контента, которые отслеживает чистильщик в темах
CONTENT_TYPES = [
    'text', 'photo', 'video', 'animation',
    'sticker', 'voice', 'audio', 'document',
    'video_note', 'poll', 'location', 'contact',
]

# =====================================================================
#  ИНИЦИАЛИЗАЦИЯ БОТА И БАЗЫ ДАННЫХ (SQLite)
# =====================================================================

bot = telebot.TeleBot(TOKEN if TOKEN else "dummy_token", parse_mode=None, threaded=True)

# Блокировка для потокобезопасного доступа к SQLite
db_lock = threading.Lock()


def db_conn() -> sqlite3.Connection:
    """Создает потокобезопасное подключение к SQLite базе данных с WAL-режимом."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    """Создает таблицы базы данных при старте, если они не существуют."""
    with db_lock, db_conn() as conn:
        # Очередь запланированных удалений (сохраняется при перезагрузках)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_deletions (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            thread_id INTEGER,
            delete_at REAL NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        """)

        # Конфигурация (динамические параметры)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        # Отслеживаемые темы форума
        conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            topic_id INTEGER PRIMARY KEY,
            type TEXT NOT NULL  -- 'rp' или 'offtop'
        );
        """)

        # Статистика
        conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        """)

        # Начальные дефолтные значения
        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('deleted_total', 0);")
        conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('started_at', ?);", (int(time.time()),))
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('rp_timer_seconds', '2400');")  # 40 минут
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_min_words', '1');")
        conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('offtop_max_words', '10');")
        conn.commit()


# Инициализируем БД сразу на уровне модуля
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


def db_add_pending(chat_id: int, message_id: int, thread_id: Optional[int], delete_at: float):
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_deletions (chat_id, message_id, thread_id, delete_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, thread_id, delete_at, time.time())
        )
        conn.commit()


def db_remove_pending(chat_id: int, message_id: int):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM pending_deletions WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        conn.commit()


def db_all_pending() -> List[Tuple[int, int, Optional[int], float]]:
    with db_lock, db_conn() as conn:
        return conn.execute("SELECT chat_id, message_id, thread_id, delete_at FROM pending_deletions ORDER BY delete_at ASC").fetchall()


def db_count_pending() -> int:
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()
        return int(row[0]) if row else 0


def db_get_expired_pending(now_ts: float) -> List[Tuple[int, int]]:
    with db_lock, db_conn() as conn:
        return conn.execute("SELECT chat_id, message_id FROM pending_deletions WHERE delete_at <= ?", (now_ts,)).fetchall()


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
    """Синхронизирует переменные в оперативной памяти с базой SQLite."""
    global RP_TIMER_SECONDS, OFFTOP_MIN_WORDS, OFFTOP_MAX_WORDS, RP_TOPICS, OFFTOP_TOPICS
    with cache_lock:
        RP_TIMER_SECONDS = int(get_config('rp_timer_seconds', '2400') or '2400')
        OFFTOP_MIN_WORDS = int(get_config('offtop_min_words', '1') or '1')
        OFFTOP_MAX_WORDS = int(get_config('offtop_max_words', '10') or '10')
        RP_TOPICS = {row[0] for row in get_topics_from_db('rp')}
        OFFTOP_TOPICS = {row[0] for row in get_topics_from_db('offtop')}


def all_monitored_topics() -> Set[int]:
    with cache_lock:
        return RP_TOPICS | OFFTOP_TOPICS


# Первичная загрузка кэша
reload_cache()

# Хранилище активных потоковых таймеров: {(chat_id, message_id): threading.Timer}
active_timers: Dict[Tuple[int, int], threading.Timer] = {}
timers_lock = threading.Lock()

# Хранилище состояний ожидания ввода от администратора: {admin_id: {'action': str, ...}}
admin_states: Dict[int, Dict[str, Any]] = {}
states_lock = threading.Lock()

BOT_START_TIMESTAMP = time.time()

# =====================================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ЛОГИРОВАНИЕ
# =====================================================================

def is_admin(user_id: Optional[int]) -> bool:
    """Проверяет, является ли пользователь администратором бота."""
    return bool(user_id and user_id in ADMIN_IDS)


def format_seconds(seconds: int) -> str:
    """Форматирует секунды в читаемый вид (напр. '40 мин', '10 мин 30 сек')."""
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
    """Форматирует аптайм бота."""
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
    """
    Отправляет лог в лог-группу и дублирует в консоль.
    Ошибки отправки ловятся тихо и не ломают работу бота.
    """
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
    """
    Безопасно удаляет сообщение в Telegram.
    Удаляет запись из базы данных и очищает активный таймер.
    Возвращает True в случае успешного удаления.
    """
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


def schedule_deletion(chat_id: int, message_id: int, thread_id: Optional[int], delay_seconds: float, persist: bool = True):
    """
    Планирует удаление сообщения через заданное количество секунд.
    Сохраняет задание в SQLite для переживания перезапусков.
    """
    key = (chat_id, message_id)
    delete_at = time.time() + max(0.0, delay_seconds)

    if persist:
        db_add_pending(chat_id, message_id, thread_id, delete_at)

    def _timer_callback():
        delete_message_safe(chat_id, message_id)

    with timers_lock:
        old_timer = active_timers.pop(key, None)
        if old_timer:
            old_timer.cancel()

        timer = threading.Timer(max(0.1, delay_seconds), _timer_callback)
        timer.daemon = True
        active_timers[key] = timer
        timer.start()


def cancel_scheduled_deletion(chat_id: int, message_id: int):
    """Отменяет запланированное удаление (например, если в сообщение добавили #)."""
    key = (chat_id, message_id)
    with timers_lock:
        timer = active_timers.pop(key, None)
        if timer:
            timer.cancel()
    db_remove_pending(chat_id, message_id)


def restore_queue_on_startup():
    """
    При запуске бота считывает очередь из SQLite:
    - Просроченные сообщения удаляет сразу;
    - Для остальных запускает таймеры с оставшимся временем.
    """
    rows = db_all_pending()
    now = time.time()
    restored_count = 0
    fired_now_count = 0

    for chat_id, message_id, thread_id, delete_at in rows:
        remaining = delete_at - now
        if remaining <= 0:
            delete_message_safe(chat_id, message_id)
            fired_now_count += 1
        else:
            schedule_deletion(chat_id, message_id, thread_id, remaining, persist=False)
            restored_count += 1

    if rows:
        log_event(
            f"🔄 <b>Восстановление после рестарта:</b>\n"
            f"• Возобновлено таймеров: <code>{restored_count}</code>\n"
            f"• Просроченных удалено сразу: <code>{fired_now_count}</code>"
        )


def watchdog_background_worker():
    """
    Фоновый сторожевой поток: каждые 30 секунд проверяет БД на предмет
    зависших или просроченных сообщений (на случай сетевых сбоев).
    """
    while True:
        try:
            time.sleep(30)
            now = time.time()
            expired = db_get_expired_pending(now)
            if expired:
                for chat_id, message_id in expired:
                    delete_message_safe(chat_id, message_id)
                    time.sleep(0.05)
        except Exception as e:
            print(f"[{get_current_time_str()}] Ошибка в watchdog_worker: {e}")

# =====================================================================
#  АДМИН-ПАНЕЛЬ: ГЕНЕРАТОРЫ МЕНЮ И КЛАВИАТУР
# =====================================================================

def build_admin_main_text() -> str:
    """Генерирует главный информационный текст админ-панели."""
    reload_cache()
    rp_sorted = sorted(RP_TOPICS)
    offtop_sorted = sorted(OFFTOP_TOPICS)

    rp_str = ", ".join(f"<code>{tid}</code>" for tid in rp_sorted) if rp_sorted else "<i>нет тем</i>"
    offtop_str = ", ".join(f"<code>{tid}</code>" for tid in offtop_sorted) if offtop_sorted else "<i>нет тем</i>"

    pending_cnt = db_count_pending()
    deleted_cnt = get_deleted_stat()
    uptime_str = format_uptime(time.time() - BOT_START_TIMESTAMP)

    text = (
        "🤖 <b>Панель управления Cleaner Bot</b>\n\n"
        f"⚡ <b>Статус:</b> <code>Активен 🟢</code> | <b>Аптайм:</b> <code>{uptime_str}</code>\n"
        f"⏱ <b>Таймер RP:</b> <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n"
        f"📝 <b>Лимит Оффтопа:</b> <code>{OFFTOP_MIN_WORDS} – {OFFTOP_MAX_WORDS} слов</code>\n\n"
        f"🗂 <b>РП-темы:</b> {rp_str}\n"
        f"🗑 <b>Оффтоп-темы:</b> {offtop_str}\n\n"
        f"⏳ <b>В очереди на удаление:</b> <code>{pending_cnt}</code> сообщений\n"
        f"📊 <b>Всего удалено за все время:</b> <code>{deleted_cnt}</code>\n\n"
        "<i>Используйте кнопки ниже для управления ботом:</i>"
    )
    return text


def build_admin_main_keyboard() -> types.InlineKeyboardMarkup:
    """Создает инлайн-клавиатуру главного меню админки."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⏱ Изменить таймер RP", callback_data="adm:timer"),
        types.InlineKeyboardButton("🧹 Очистить очередь", callback_data="adm:purge_confirm"),
    )
    kb.add(
        types.InlineKeyboardButton("➕ РП-тема", callback_data="adm:add_rp"),
        types.InlineKeyboardButton("➕ Офтоп-тема", callback_data="adm:add_offtop"),
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
    """Кнопка отмены ввода."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="adm:cancel_input"))
    return kb


def build_remove_topics_keyboard() -> types.InlineKeyboardMarkup:
    """Создает клавиатуру со списком тем для удаления в 1 клик."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    all_topics = get_topics_from_db()

    for tid, ttype in all_topics:
        icon = "🗂 РП" if ttype == 'rp' else "🗑 Оффтоп"
        kb.add(types.InlineKeyboardButton(f"❌ {icon}: {tid}", callback_data=f"adm:del_topic:{tid}"))

    kb.add(types.InlineKeyboardButton("✍️ Ввести ID темы вручную", callback_data="adm:remove_manual"))
    kb.add(types.InlineKeyboardButton("◀️ Назад в меню", callback_data="adm:main_menu"))
    return kb

# =====================================================================
#  ОБРАБОТЧИКИ ДЛЯ АДМИНИСТРАТОРА (КОМАНДЫ И ВВОД)
# =====================================================================

@bot.message_handler(commands=['start', 'admin', 'menu', 'panel', 'status', 'stats', 'purge'])
def handle_admin_commands(message: types.Message):
    """
    Обработчик команд администратора.
    НЕ-администраторы полностью игнорируются (бот им вообще не отвечает).
    """
    user_id = message.from_user.id if message.from_user else 0
    if not is_admin(user_id):
        return  # Полное игнорирование обычных пользователей

    # Очищаем зависшие состояния ввода
    with states_lock:
        admin_states.pop(user_id, None)

    # Отправляем главное меню
    bot.send_message(
        message.chat.id,
        build_admin_main_text(),
        parse_mode='HTML',
        reply_markup=build_admin_main_keyboard()
    )


@bot.message_handler(func=lambda m: is_admin(m.from_user.id if m.from_user else 0) and (m.from_user.id in admin_states), content_types=['text'])
def handle_admin_text_input(message: types.Message):
    """
    Обрабатывает текстовый ввод от администратора (когда бот ждет число, ID темы и т.д.).
    """
    user_id = message.from_user.id
    with states_lock:
        state = admin_states.pop(user_id, None)

    if not state:
        return

    action = state.get('action')
    text = (message.text or "").strip()

    # Если админ написал /cancel или отмена
    if text.lower() in ('/cancel', 'отмена', 'cancel'):
        bot.send_message(message.chat.id, "❌ Действие отменено.", reply_markup=build_admin_main_keyboard())
        return

    # 1. Изменение таймера RP
    if action == 'set_timer':
        try:
            val_str = text.replace(',', '.')
            minutes = float(val_str)
            if minutes <= 0:
                raise ValueError("Время должно быть положительным")
            seconds = int(minutes * 60)
            if seconds < 5:
                bot.send_message(message.chat.id, "❌ Минимальный таймер: 5 секунд (0.1 мин). Попробуйте еще раз:")
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
                "❌ Некорректное число минут! Введите положительное число (например: <code>10</code> или <code>2.5</code>):",
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
                f"✅ <b>Тема {topic_id} успешно добавлена в список RP!</b>\n"
                f"Сообщения без <code>#</code> в этой теме будут удаляться через <b>{format_seconds(RP_TIMER_SECONDS)}</b>.",
                parse_mode='HTML',
                reply_markup=build_admin_main_keyboard()
            )
            log_event(f"➕ <b>Админ [ID: {user_id}]</b> добавил тему <code>{topic_id}</code> в <b>RP</b>.")
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
                f"✅ <b>Тема {topic_id} успешно добавлена в список ОФФТОП!</b>\n"
                f"Короткие сообщения ({OFFTOP_MIN_WORDS}-{OFFTOP_MAX_WORDS} слов) без <code>#</code> удаляются мгновенно.",
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

    # 5. Настройка лимитов слов оффтопа
    elif action == 'set_offtop_limits':
        parts = text.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            min_w = int(parts[0])
            max_w = int(parts[1])
            if min_w <= max_w and min_w >= 1:
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
            "❌ Неверный формат! Введите два числа через пробел (минимум и максимум, например: <code>1 10</code>):",
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
        # Игнорируем нажатия от не-админов
        bot.answer_callback_query(call.id)
        return

    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # 1. Главное меню / Назад / Обновить
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

    # 2. Закрыть меню
    elif data == "adm:close":
        with states_lock:
            admin_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "Панель закрыта")
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    # 3. Отмена текущего ввода
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

    # 4. Запрос на изменение таймера
    elif data == "adm:timer":
        with states_lock:
            admin_states[user_id] = {'action': 'set_timer'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⏱ <b>Настройка таймера RP-тем</b>\n\n"
            f"Текущее значение: <b>{format_seconds(RP_TIMER_SECONDS)}</b>\n\n"
            f"Пришлите новое время в <b>минутах</b> (например: <code>10</code>, <code>40</code> или <code>0.5</code>):",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 5. Запрос на добавление RP темы
    elif data == "adm:add_rp":
        with states_lock:
            admin_states[user_id] = {'action': 'add_rp'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"➕ <b>Добавление RP-темы</b>\n\n"
            f"Пришлите <b>ID темы (thread_id)</b> форума:\n"
            f"<i>(Сообщения без # в ней будут удаляться через {format_seconds(RP_TIMER_SECONDS)})</i>",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 6. Запрос на добавление Оффтоп темы
    elif data == "adm:add_offtop":
        with states_lock:
            admin_states[user_id] = {'action': 'add_offtop'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"➕ <b>Добавление Оффтоп-темы</b>\n\n"
            f"Пришлите <b>ID темы (thread_id)</b> форума:\n"
            f"<i>(Короткие сообщения от {OFFTOP_MIN_WORDS} до {OFFTOP_MAX_WORDS} слов без # будут удаляться сразу)</i>",
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
                "ℹ️ <b>Список тем пуст!</b> Сначала добавьте РП или Оффтоп тему.",
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

    # 8. Удаление темы по нажатию на кнопку
    elif data.startswith("adm:del_topic:"):
        topic_id_str = data.split(":")[-1]
        try:
            topic_id = int(topic_id_str)
            remove_topic_from_db(topic_id)
            reload_cache()
            bot.answer_callback_query(call.id, f"✅ Тема {topic_id} удалена!")
            log_event(f"➖ <b>Админ [ID: {user_id}]</b> удалил тему <code>{topic_id}</code>.")

            # Обновляем меню удаления
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
        rp_list = "\n".join(f"• <code>{tid}</code>" for tid in sorted(RP_TOPICS)) or "<i>(нет РП-тем)</i>"
        offtop_list = "\n".join(f"• <code>{tid}</code>" for tid in sorted(OFFTOP_TOPICS)) or "<i>(нет оффтоп-тем)</i>"

        text = (
            "📋 <b>Список отслеживаемых тем:</b>\n\n"
            f"🗂 <b>РП-темы (автоудаление через {format_seconds(RP_TIMER_SECONDS)}):</b>\n{rp_list}\n\n"
            f"🗑 <b>Оффтоп-темы (мгновенно, {OFFTOP_MIN_WORDS}-{OFFTOP_MAX_WORDS} слов):</b>\n{offtop_list}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("➕ Добавить РП", callback_data="adm:add_rp"),
            types.InlineKeyboardButton("➕ Добавить Оффтоп", callback_data="adm:add_offtop"),
        )
        kb.add(types.InlineKeyboardButton("◀️ В главное меню", callback_data="adm:main_menu"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', reply_markup=kb)

    # 11. Настройка лимитов оффтопа
    elif data == "adm:offtop_limits":
        with states_lock:
            admin_states[user_id] = {'action': 'set_offtop_limits'}
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⚙️ <b>Настройка лимитов слов для Оффтопа</b>\n\n"
            f"Текущие лимиты: от <b>{OFFTOP_MIN_WORDS}</b> до <b>{OFFTOP_MAX_WORDS}</b> слов.\n\n"
            f"Пришлите два числа через пробел (минимум и максимум), например: <code>1 10</code> или <code>1 5</code>:",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=build_cancel_keyboard()
        )

    # 12. Подробная статистика
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
            f"⏳ <b>В очереди на удаление (SQLite):</b> <code>{pending_db}</code>\n"
            f"⚡ <b>Активных таймеров в RAM:</b> <code>{active_ram_timers}</code>\n"
            f"🗂 <b>Количество РП-тем:</b> <code>{len(RP_TOPICS)}</code>\n"
            f"🗑 <b>Количество Оффтоп-тем:</b> <code>{len(OFFTOP_TOPICS)}</code>\n"
            f"⏱ <b>Таймер RP:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>\n"
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

    # 13. Подтверждение очистки очереди
    elif data == "adm:purge_confirm":
        pending_count = db_count_pending()
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("🧹 ДА, УДАЛИТЬ ВСЁ", callback_data="adm:purge_exec"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="adm:main_menu")
        )
        bot.edit_message_text(
            f"⚠️ <b>Подтверждение очистки очереди</b>\n\n"
            f"В очереди сейчас находится <b>{pending_count}</b> сообщений.\n"
            f"Вы действительно хотите принудительно удалить их прямо сейчас?",
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            reply_markup=kb
        )

    # 14. Выполнение очистки очереди
    elif data == "adm:purge_exec":
        bot.answer_callback_query(call.id, "🧹 Запуск очистки...")

        # Отменяем все таймеры в памяти
        with timers_lock:
            for timer in active_timers.values():
                timer.cancel()
            active_timers.clear()

        # Забираем все сообщения из базы
        rows = db_clear_all_pending()
        deleted_count = 0
        failed_count = 0

        for chat_to_del, msg_to_del in rows:
            try:
                bot.delete_message(chat_to_del, msg_to_del)
                deleted_count += 1
            except Exception:
                failed_count += 1
            time.sleep(0.04)  # Предотвращение Telegram rate-limit

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
    func=lambda m: m.chat.type in ('group', 'supergroup') and m.message_thread_id in all_monitored_topics(),
    content_types=CONTENT_TYPES
)
def handle_group_message(message: types.Message):
    """
    Основной обработчик сообщений в отслеживаемых темах групп.
    Работает беззвучно, никогда не отвечает обычным пользователям.
    """
    thread_id = message.message_thread_id
    if not thread_id:
        return

    text = message.text or message.caption or ""

    # ПРАВИЛО: Сообщения с хэштегом # НЕ удаляются
    if '#' in text:
        return

    # 1. Если тема является РП-темой
    if thread_id in RP_TOPICS:
        schedule_deletion(message.chat.id, message.message_id, thread_id, RP_TIMER_SECONDS)
        return

    # 2. Если тема является Оффтоп-темой
    if thread_id in OFFTOP_TOPICS:
        word_count = len(text.split()) if text else 0
        # Если количество слов укладывается в диапазон оффтопа (по умолчанию 1-10 слов)
        if OFFTOP_MIN_WORDS <= word_count <= OFFTOP_MAX_WORDS:
            delete_message_safe(message.chat.id, message.message_id)


@bot.edited_message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and m.message_thread_id in all_monitored_topics(),
    content_types=CONTENT_TYPES
)
def handle_group_edited_message(message: types.Message):
    """
    Обработчик отредактированных сообщений в темах.
    Если пользователь добавил # — отменяем удаление.
    Если пользователь убрал # — запускаем удаление.
    """
    thread_id = message.message_thread_id
    if not thread_id:
        return

    text = message.text or message.caption or ""

    if '#' in text:
        # Добавили хэштег — отменяем таймер удаления
        cancel_scheduled_deletion(message.chat.id, message.message_id)
    else:
        # Хэштега нет — если это РП-тема, планируем удаление (если еще не запланировано)
        if thread_id in RP_TOPICS:
            key = (message.chat.id, message.message_id)
            with timers_lock:
                already_scheduled = key in active_timers
            if not already_scheduled:
                schedule_deletion(message.chat.id, message.message_id, thread_id, RP_TIMER_SECONDS)

        elif thread_id in OFFTOP_TOPICS:
            word_count = len(text.split()) if text else 0
            if OFFTOP_MIN_WORDS <= word_count <= OFFTOP_MAX_WORDS:
                delete_message_safe(message.chat.id, message.message_id)

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
    print(f"  • РП-темы: {sorted(RP_TOPICS) or 'нет'}")
    print(f"  • Оффтоп-темы: {sorted(OFFTOP_TOPICS) or 'нет'}")
    print(f"  • Таймер RP: {format_seconds(RP_TIMER_SECONDS)}")

    # 1. Восстанавливаем сохраненную очередь из базы данных
    restore_queue_on_startup()

    # 2. Запускаем фоновый сторожевой поток (watchdog)
    watchdog_thread = threading.Thread(target=watchdog_background_worker, daemon=True, name="WatchdogWorker")
    watchdog_thread.start()

    # 3. Отправляем уведомление о старте в лог-группу
    log_event(
        f"🚀 <b>Cleaner Bot успешно запущен!</b>\n\n"
        f"⏱ <b>Таймер RP:</b> <code>{format_seconds(RP_TIMER_SECONDS)}</code>\n"
        f"📝 <b>Лимит Оффтопа:</b> <code>{OFFTOP_MIN_WORDS}-{OFFTOP_MAX_WORDS} слов</code>\n"
        f"🗂 <b>РП-темы:</b> <code>{sorted(RP_TOPICS) or '—'}</code>\n"
        f"🗑 <b>Оффтоп-темы:</b> <code>{sorted(OFFTOP_TOPICS) or '—'}</code>"
    )

    # 4. Бесконечный цикл Polling с защитой от разрывов сети
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
