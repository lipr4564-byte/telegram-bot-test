import os
import threading
import telebot

# Считываем токен из настроек хостинга
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана на хостинге!")

bot = telebot.TeleBot(TOKEN)

# Считываем ID владельца из настроек хостинга (0 — если не задан)
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# Список твоих тем
TARGET_TOPICS = [4, 6, 12, 14]

# Функция для отложенного удаления
def delete_delayed(chat_id, message_id, delay_seconds):
    def _delete():
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    
    # Запускаем таймер, который выполнит функцию _delete через delay_seconds
    timer = threading.Timer(delay_seconds, _delete)
    timer.start()

@bot.message_handler(content_types=['text'])
def filter_offtop(message):
    # Проверяем тему
    if message.message_thread_id not in TARGET_TOPICS:
        return

    text = message.text
    if not text:
        return

    # 1. Если есть '#' — вообще не трогаем сообщение
    if '#' in text:
        return

    # 2. Если есть '//' — запускаем таймер на 40 минут (2400 секунд)
    if '//' in text:
        delete_delayed(message.chat.id, message.message_id, 2400)
        return

    # 3. Обычные сообщения без // и #
    # Считаем слова: если от 1 до 10 - удаляем СРАЗУ
    word_count = len(text.split())
    if 1 <= word_count <= 10:
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

@bot.message_handler(commands=['status'])
def check_status(message):
    if message.from_user.id == ADMIN_ID:
        bot.send_message(message.chat.id, "Бот-чистильщик работает.\nКороткий оффтоп — сразу в мусорку.\nОффтоп с '//' — удаляется через 40 минут.")

if __name__ == '__main__':
    print("Чистильщик запущен. Таймер на 40 минут активирован...")
    bot.infinity_polling(none_stop=True)
