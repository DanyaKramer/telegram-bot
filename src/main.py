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
from requests import exceptions as req_exceptions
import re

from mpt_schedule_client import MptScheduleClient

# --- Глобальные переменные ---
task = BackgroundScheduler()
bot = telebot.TeleBot(config.token)
users = set()

# Кэш для быстрых ответов
cache = {
    "date": "Дата не загружена",
    "replacements": "Данных пока нет",
    "replacements_map": {},
    "schedule_by_day": {},
}

GROUP_QUERY = "СА-1-23; СА-11/1-24"
WEEK_DAYS = {
    "понедельник": "Понедельник",
    "вторник": "Вторник",
    "среда": "Среда",
    "четверг": "Четверг",
    "пятница": "Пятница",
    "суббота": "Суббота",
}


def resolve_log_file_path():
    candidates = [
        os.path.join("/app", "logs", "bot_errors.log"),
        os.path.join(os.getcwd(), "logs", "bot_errors.log"),
    ]
    for path in candidates:
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
            return path
        except OSError:
            continue
    return "bot_errors.log"


LOG_FILE_PATH = resolve_log_file_path()

logging.basicConfig(
    filename=LOG_FILE_PATH,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)


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

    elif message.text == "Изменения в расписании":
        handle_text(message)

    elif message.text == "📂 Скачать лог":
        log_path = LOG_FILE_PATH
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

    if not message.text:
        bot.send_message(message.chat.id, "⚠️ Пустое сообщение — рассылка отменена.")
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


def get_replacements_map(soup, target_text="СА-1-23"):
    """Возвращает словарь замен в формате {номер_пары: текст_замены}."""
    groups = soup.find_all('div', class_='table-responsive')
    replacements_map = {}

    for group in groups:
        if target_text.lower() not in group.text.lower():
            continue

        table = group.find('table')
        if not table:
            continue

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 3:
                continue

            lesson = cells[0].text.strip()
            replaced = cells[1].text.strip()
            replacement = cells[2].text.strip()

            match = re.search(r"\d+", lesson)
            if not match:
                continue

            lesson_number = match.group(0)
            replacements_map[lesson_number] = f"{replaced} → {replacement}"

    return replacements_map


def parse_schedule_by_day(section_text):
    """Разбирает текст расписания группы и возвращает словарь по дням недели."""
    schedule = {day: [] for day in WEEK_DAYS}
    current_day = None

    for raw_line in section_text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue

        day_match = re.match(
            r'^(понедельник|вторник|среда|четверг|пятница|суббота)\b[\s:,-]*(.*)$',
            line,
            flags=re.IGNORECASE,
        )
        if day_match:
            current_day = day_match.group(1).lower()
            rest = day_match.group(2).strip()
            if rest:
                schedule[current_day].append(rest)
            continue

        if current_day:
            schedule[current_day].append(line)

    return schedule


def load_group_schedule(group_query=GROUP_QUERY):
    """Загружает расписание выбранной группы и возвращает словарь по дням."""
    client = MptScheduleClient()
    soup = client.fetch_page()
    targets = client.collect_anchors(soup)
    group = client.find_target(targets, group_query)
    if not group:
        return {day: [] for day in WEEK_DAYS}

    section_text = client.extract_section_text(soup, group.anchor_id)
    if not section_text:
        return {day: [] for day in WEEK_DAYS}

    return parse_schedule_by_day(section_text)


def build_day_schedule_message(day_key):
    day_title = WEEK_DAYS[day_key]
    lessons = cache.get("schedule_by_day", {}).get(day_key, [])

    if not lessons:
        return f"📅 {day_title}\n\nВ этот день пар нет, выходной 🎉"

    replacements_map = cache.get("replacements_map", {})
    lines = [f"📅 {day_title}"]
    for lesson in lessons:
        match = re.search(r"\b(\d+)\b", lesson)
        replacement_text = ""
        if match and match.group(1) in replacements_map:
            replacement_text = f" ↪ Замена: {replacements_map[match.group(1)]}"
        lines.append(f"• {lesson}{replacement_text}")

    return "\n".join(lines)


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
        cache["replacements_map"] = get_replacements_map(soup)
        cache["schedule_by_day"] = load_group_schedule()
        logger.info("Кэш обновлён: %s", cache["date"])
    except Exception as e:
        logger.error("Ошибка обновления кэша: %s", e, exc_info=True)


# --- Хэндлеры бота ---
@bot.message_handler(commands=['start'])
def start(message):
    users.add(message.chat.id)
    save_users()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Изменения в расписании"))
    markup.add(types.KeyboardButton("📅 Расписание по дням"))
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
    elif message.text == "📅 Расписание по дням":
        day_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        day_markup.add(
            types.KeyboardButton("Понедельник"),
            types.KeyboardButton("Вторник"),
            types.KeyboardButton("Среда"),
            types.KeyboardButton("Четверг"),
            types.KeyboardButton("Пятница"),
            types.KeyboardButton("Суббота"),
        )
        bot.send_message(
            message.chat.id,
            "Выберите день недели, чтобы посмотреть расписание:",
            reply_markup=day_markup,
        )
    elif message.text and message.text.lower() in WEEK_DAYS:
        day_key = message.text.lower()
        bot.send_message(message.chat.id, build_day_schedule_message(day_key))


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
        logger.info("Периодическая проверка завершена")
    except Exception as e:
        logger.error("Ошибка фоновой задачи: %s", e, exc_info=True)


# --- Планировщик ---
task.add_job(safe_send, 'interval', minutes=20)
task.start()

# Первое обновление сразу
update_cache()

# --- Запуск бота ---
def notify_admin(message_text):
    if not getattr(config, "ADMIN_ID", None):
        logger.warning("ADMIN_ID не задан, уведомление пропущено.")
        return
    try:
        bot.send_message(
            config.ADMIN_ID,
            message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        logging.error("Не удалось уведомить админа:\n%s", traceback.format_exc())


def run_polling():
    delay = 5
    max_delay = 300
    while True:
        try:
            logger.info("Запуск polling...")
            bot.polling(none_stop=True, interval=1, timeout=30, long_polling_timeout=30)
        except (req_exceptions.ReadTimeout, req_exceptions.ConnectTimeout, req_exceptions.ConnectionError) as e:
            logging.error("Сетевая ошибка в polling:\n%s", traceback.format_exc())
            notify_admin(f"⚠️ Сетевая ошибка polling:\n\n<pre>{e}</pre>")
        except Exception as e:
            logging.error("Неожиданная ошибка в polling:\n%s", traceback.format_exc())
            notify_admin(f"⚠️ Бот упал с ошибкой:\n\n<pre>{e}</pre>")
        else:
            delay = 5
            continue

        logger.info("Перезапуск polling через %s секунд...", delay)
        time.sleep(delay)
        delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    load_users()
    logger.info("Бот запущен. Активных пользователей: %d", len(users))
    run_polling()
