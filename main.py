import os
import telebot

# Считываем токен из переменной окружения на хостинге
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана на хостинге!")

bot = telebot.TeleBot(TOKEN)

# Считываем ID владельца из переменной окружения (или используем твой по умолчанию)
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# ID тем (взяты из последних цифр твоих ссылок: .../4, .../6, .../14)
TARGET_TOPICS = [4, 6, 14]

@bot.message_handler(content_types=['text'])
def filter_offtop(message):
    # 1. Проверяем, находится ли сообщение в нужной теме
    if message.message_thread_id not in TARGET_TOPICS:
        return

    # Получаем текст сообщения
    text = message.text
    if not text:
        return

    # 2. Проверяем наличие исключений: '//' или '#'
    if '//' in text or '#' in text:
        return

    # 3. Считаем количество слов (разделяем текст по пробелам)
    word_count = len(text.split())

    # 4. Если слов от 1 до 10 (включительно) - удаляем
    if 1 <= word_count <= 10:
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            # Ошибка может возникнуть, если у бота нет прав на удаление
            pass

# Небольшая команда для проверки статуса (работает ТОЛЬКО для тебя)
@bot.message_handler(commands=['status'])
def check_status(message):
    if message.from_user.id == ADMIN_ID:
        bot.send_message(message.chat.id, "Бот работает и следит за оффтопом.")

# Запуск бота в бесконечном цикле
if __name__ == '__main__':
    print("Бот запущен...")
    bot.infinity_polling(none_stop=True)
