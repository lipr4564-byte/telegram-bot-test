import os
import time
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

ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# Темы для РП — любые сообщения без # удаляются через 40 минут
RP_TOPICS = [4, 6, 12, 14, 10, 16]

# Темы для оффтопа — короткие сообщения без # удаляются сразу
OFFTOP_TOPICS = []

ALL_TOPICS = set(RP_TOPICS + OFFTOP_TOPICS)

# Счётчик удалений
deleted_count = 0
deleted_lock = threading.Lock()

# Активные таймеры: {(chat_id, message_id): Timer}
active_timers = {}
timers_lock = threading.Lock()

# ─── Утилиты ──────────────────────────────────────────────────────────────────
def delete_delayed(chat_id, message_id, delay_seconds):
    key = (chat_id, message_id)

    def _delete():
        global deleted_count
        try:
            bot.delete_message(chat_id, message_id)
            with deleted_lock:
                deleted_count += 1
        except Exception:
            pass
        with timers_lock:
            active_timers.pop(key, None)

    timer = threading.Timer(delay_seconds, _delete)
    with timers_lock:
        active_timers[key] = timer
    timer.start()


def cancel_timer(chat_id, message_id):
    key = (chat_id, message_id)
    with timers_lock:
        timer = active_timers.pop(key, None)
    if timer:
        timer.cancel()


def is_rp_topic(thread_id):
    return thread_id in RP_TOPICS


def is_offtop_topic(thread_id):
    return thread_id in OFFTOP_TOPICS


# ─── Обработчики сообщений ────────────────────────────────────────────────────
CONTENT_TYPES = [
    'text', 'photo', 'video', 'animation',
    'sticker', 'voice', 'audio', 'document',
    'video_note', 'poll', 'location', 'contact',
]


@bot.message_handler(content_types=CONTENT_TYPES)
def handle_message(message):
    thread_id = message.message_thread_id
    if thread_id not in ALL_TOPICS:
        return

    text = message.text or message.caption or ""

    if '#' in text:
        return

    if is_rp_topic(thread_id):
        delete_delayed(message.chat.id, message.message_id, 2400)
        return

    if is_offtop_topic(thread_id):
        word_count = len(text.split()) if text else 0
        if 1 <= word_count <= 10:
            try:
                global deleted_count
                bot.delete_message(message.chat.id, message.message_id)
                with deleted_lock:
                    deleted_count += 1
            except Exception:
                pass


@bot.edited_message_handler(content_types=CONTENT_TYPES)
def handle_edit(message):
    thread_id = message.message_thread_id
    if thread_id not in ALL_TOPICS:
        return

    text = message.text or message.caption or ""

    if '#' in text:
        cancel_timer(message.chat.id, message.message_id)
    else:
        if is_rp_topic(thread_id):
            key = (message.chat.id, message.message_id)
            with timers_lock:
                already = key in active_timers
            if not already:
                delete_delayed(message.chat.id, message.message_id, 2400)


# ─── Команды ──────────────────────────────────────────────────────────────────
@bot.message_handler(commands=['status'])
def cmd_status(message):
    if message.from_user.id != ADMIN_ID:
        return
    with timers_lock:
        pending = len(active_timers)
    with deleted_lock:
        total = deleted_count
    bot.send_message(
        message.chat.id,
        "🤖 <b>Чистильщик активен</b>\n\n"
        f"🗂 РП-темы: {RP_TOPICS}\n"
        f"🗑 Оффтоп-темы: {OFFTOP_TOPICS}\n\n"
        f"⏳ Ожидают удаления: <b>{pending}</b> сообщений\n"
        f"✅ Удалено за сессию: <b>{total}</b>\n\n"
        "Правила:\n"
        "• Сообщения с <code>#</code> — неприкосновенны\n"
        "• РП-темы: всё без <code>#</code> → удаление через 40 минут\n"
        "• Оффтоп-темы: текст ≤10 слов без <code>#</code> → мгновенное удаление",
        parse_mode='HTML'
    )


@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if message.from_user.id != ADMIN_ID:
        return
    with deleted_lock:
        total = deleted_count
    with timers_lock:
        pending = len(active_timers)
    bot.send_message(
        message.chat.id,
        f"📊 <b>Статистика</b>\n"
        f"Удалено за сессию: <b>{total}</b>\n"
        f"В очереди на удаление: <b>{pending}</b>",
        parse_mode='HTML'
    )


@bot.message_handler(commands=['topics'])
def cmd_topics(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(
        message.chat.id,
        f"📋 <b>Отслеживаемые темы</b>\n"
        f"РП: {RP_TOPICS}\n"
        f"Оффтоп: {OFFTOP_TOPICS}",
        parse_mode='HTML'
    )


@bot.message_handler(commands=['purge'])
def cmd_purge(message):
    if message.from_user.id != ADMIN_ID:
        return
    with timers_lock:
        timers_copy = dict(active_timers)
    for (chat_id, msg_id), timer in timers_copy.items():
        timer.cancel()
        try:
            bot.delete_message(chat_id, msg_id)
            with deleted_lock:
                deleted_count += 1
        except Exception:
            pass
    with timers_lock:
        active_timers.clear()
    bot.send_message(message.chat.id, "🧹 Все отложенные сообщения удалены немедленно.")


# ─── Запуск ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Чистильщик запущен.")
    print(f"  РП-темы      : {RP_TOPICS}")
    print(f"  Оффтоп-темы  : {OFFTOP_TOPICS}")
    print("  Правило РП   : всё без '#' -> удаление через 40 мин")

    RETRY_DELAYS = [5, 10, 30, 60]
    attempt = 0

    while True:
        try:
            attempt = 0
            bot.infinity_polling(
                none_stop=True,
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
            time.sleep(delay)
            attempt += 1
