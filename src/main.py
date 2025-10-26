import requests
from bs4 import BeautifulSoup
import telebot
import config as config
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
import json
import logging
import traceback
import time
from telebot import apihelper
import os
from datetime import datetime

# --- Глобальные переменные ---
task = BackgroundScheduler()
bot = telebot.TeleBot(config.token)
users = set()

# Кэш для быстрых ответов
cache = {"date": "Дата не загружена", "replacements": "Данных пока нет"}

logging.basicConfig(
    filename="/app/logs/bot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)



# Админское меню
@bot.message_handler(commands=['admin'])
def admin_menu(message):
    if message.chat.id != config.ADMIN_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этой команды.")
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        types.KeyboardButton("📊 Список пользователей"),
        types.KeyboardButton("🔄 Обновить кэш"),
        types.KeyboardButton("🧹 Очистить пользователей"),
        types.KeyboardButton("📈 Статистика"),
        types.KeyboardButton("📂 Скачать лог"),
        types.KeyboardButton("📢 Рассылка"),
        types.KeyboardButton("⬅️ Выйти из админ-меню")
    )
    bot.send_message(message.chat.id, "⚙️ Админ-меню:", reply_markup=markup)


# Обработка кнопок админа
@bot.message_handler(func=lambda message: message.chat.id == config.ADMIN_ID)
def admin_commands(message):
    if message.text == "📊 Список пользователей":
        if not users:
            bot.send_message(message.chat.id, "👥 Список пользователей пуст.")
        else:
            text = "\n".join(str(uid) for uid in users)
            bot.send_message(message.chat.id, f"👥 Пользователи ({len(users)}):\n{text}")

    elif message.text == "🔄 Обновить кэш":
        update_cache()
        bot.send_message(message.chat.id, "✅ Кэш расписания обновлён вручную.")

    elif message.text == "🧹 Очистить пользователей":
        users.clear()
        save_users()
        bot.send_message(message.chat.id, "🧹 Список пользователей очищен.")

    elif message.text == "📈 Статистика":
        stats_text = (
            f"📊 Статистика бота:\n\n"
            f"👥 Пользователей: {len(users)}\n"
            f"🕒 Последнее обновление: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"🗂 Кэш даты: {cache.get('date', 'нет данных')}\n"
        )
        bot.send_message(message.chat.id, stats_text)

    elif message.text == "📂 Скачать лог":
        log_path = "/app/logs/bot_errors.log"
        if os.path.exists(log_path):
            with open(log_path, "rb") as log_file:
                bot.send_document(message.chat.id, log_file)
        else:
            bot.send_message(message.chat.id, "⚠️ Лог-файл не найден.")

    elif message.text == "📢 Рассылка":
        msg = bot.send_message(message.chat.id, "Введите текст рассылки:")
        bot.register_next_step_handler(msg, broadcast_message)

    elif message.text == "⬅️ Выйти из админ-меню":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("Изменения в расписании"))
        bot.send_message(message.chat.id, "↩️ Возврат в обычное меню.", reply_markup=markup)



def broadcast_message(message):
    if message.chat.id != config.ADMIN_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этой команды.")
        return

    text = message.text.strip()
    count = 0
    failed = 0

    for user_id in users.copy():
        try:
            bot.send_message(user_id, f"📢 Сообщение от администратора:\n\n{text}")
            count += 1
            time.sleep(0.1)  # защита от flood limit
        except Exception as e:
            failed += 1
            print(f"Ошибка при рассылке пользователю {user_id}: {e}")

    bot.send_message(
        config.ADMIN_ID,
        f"✅ Рассылка завершена.\n📨 Успешно: {count}\n⚠️ Ошибок: {failed}"
    )

# --- Работа с пользователями ---
def save_users():
    with open('users.json', "w", encoding="utf-8") as f:
        json.dump(list(users), f)


def load_users():
    global users
    try:
        with open("users.json", "r", encoding="utf-8") as f:
            users = set(json.load(f))
    except FileNotFoundError:
        users = set()


# --- Загрузка и парсинг страницы ---
def fetch_page():
    req = requests.get('https://mpt.ru/izmeneniya-v-raspisanii/').text
    return BeautifulSoup(req, 'lxml')


def get_replacements(soup, target_text="СА-1-23"):
    """Ищем замены по конкретной группе"""
    groups = soup.find_all('div', class_='table-responsive')
    result = []

    for group in groups:
        if target_text.lower() in group.text.lower():
            table = group.find('table')
            if not table:
                continue

            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 4:
                    lesson = cells[0].text.strip()
                    replaced = cells[1].text.strip()
                    replacement = cells[2].text.strip()
                    added = cells[3].text.strip()

                    formatted_message = (
                        f"Пара: {lesson}\n"
                        f"Что заменяют: {replaced}\n"
                        f"На что заменяют: {replacement}\n"
                        f"Добавлена: {added}\n"
                    )
                    result.append(formatted_message)

    return "\n".join(result) if result else "Возможно замен нет"


def parsing_dates(soup):
    """Возвращаем дату изменений"""
    date_tag = soup.find('h4')
    return date_tag.text.strip() if date_tag else "Дата не найдена"


# --- Обновление кэша ---
def update_cache():
    global cache
    try:
        soup = fetch_page()
        cache["date"] = parsing_dates(soup)
        cache["replacements"] = get_replacements(soup)
        print("Кэш обновлён")
    except Exception as e:
        print(f"Ошибка обновления кэша: {e}")


# --- Хэндлеры бота ---
@bot.message_handler(commands=['start'])
def start(message):
    users.add(message.chat.id)
    save_users()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Изменения в расписании"))
    bot.send_message(
        message.chat.id,
        text=f"Привет, {message.from_user.first_name}! "
             f"Я бот, который покажет тебе расписание и изменения в нём.",
        reply_markup=markup
    )


@bot.message_handler(content_types=['text'])
def handle_text(message):
    if message.text == "Изменения в расписании":
        bot.send_message(message.chat.id, f"{cache['date']}\n{cache['replacements']}")


# --- Проверка изменений и уведомления ---
last_sent_data = None


def send_notification():
    global last_sent_data
    if cache["replacements"] != last_sent_data:
        last_sent_data = cache["replacements"]
        for user_id in users:
            try:
                bot.send_message(user_id, f"Обновление в расписании:\n{cache['replacements']}")
            except telebot.apihelper.ApiTelegramException:
                print(f"Ошибка отправки пользователю {user_id}")


def safe_send():
    try:
        update_cache()
        send_notification()
        print("Проверка завершена")
    except Exception as e:
        print(f"Ошибка: {e}")


# --- Планировщик ---
task.add_job(safe_send, 'interval', minutes=20)
task.start()

# Первое обновление сразу
update_cache()

# --- Запуск бота ---
if __name__ == "__main__":
    load_users()
    try:
        print("Бот запущен...")
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        # логируем в файл
        error_text = traceback.format_exc()
        logging.error("Ошибка в polling:\n%s", error_text)

        # уведомляем админа
        try:
            bot.send_message(
                ADMIN_ID,
                f"⚠️ Бот упал с ошибкой:\n\n<pre>{e}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            # если бот не может даже отправить сообщение — записываем в лог
            logging.error("Не удалось уведомить админа:\n%s", traceback.format_exc())

        print(f"Ошибка polling: {e}")
        print("Бот завершил работу. Docker перезапустит контейнер.")
        time.sleep(5)  # небольшая пауза перед выходом