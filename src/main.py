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
import threading
from telebot import apihelper
import os
from datetime import datetime
from copy import deepcopy
from requests import exceptions as req_exceptions
import re
import html

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
    "day_messages": {},
    "last_cache_update": None,
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

CACHE_FILE_PATH = os.path.join(os.getcwd(), "cache_data.json")
SCHEDULE_CACHE_TTL_SECONDS = 600
# Время, после которого при новом запросе расписания отправляется новое сообщение вместо редактирования
SCHEDULE_MESSAGE_TTL_SECONDS = 6 * 60  # 6 минут

# Состояние сообщения расписания по чатам: chat_id -> {message_id, sent_at}
schedule_message_state = {}

# --- Rate limiting (защита от флуда) ---
user_request_timestamps = {}  # user_id -> list of timestamps
RATE_LIMIT_WINDOW_SEC = 10
RATE_LIMIT_MAX_REQUESTS = 5


def _check_rate_limit(user_id, chat_id, from_user):
    """Возвращает True если лимит превышен (и отправляет сообщение), False если ок."""
    now = time.time()
    key = user_id or chat_id
    if key not in user_request_timestamps:
        user_request_timestamps[key] = []
    ts_list = user_request_timestamps[key]
    ts_list[:] = [t for t in ts_list if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(ts_list) >= RATE_LIMIT_MAX_REQUESTS:
        bot.send_message(chat_id, "Пошел нахуй")
        return True
    ts_list.append(now)
    return False



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
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
    logger.addHandler(console_handler)

# --- Дебаг: логируем каждый входящий апдейт ---
def debug_updates_listener(updates):
    """Логируем входящие апдейты для отладки."""
    for u in updates:
        if hasattr(u, "message") and u.message:
            m = u.message
            logger.debug(
                "[IN] chat_id=%s user_id=%s text=%r",
                m.chat.id,
                m.from_user.id if m.from_user else None,
                getattr(m, "text", None) or "(no text)",
            )


# --- Inline-кнопки выбора дня (на сообщении) ---
def get_schedule_days_inline_markup():
    """Кнопки выбора дня недели на сообщении (inline)."""
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(
        types.InlineKeyboardButton("Пн", callback_data="day:понедельник"),
        types.InlineKeyboardButton("Вт", callback_data="day:вторник"),
        types.InlineKeyboardButton("Ср", callback_data="day:среда"),
    )
    markup.row(
        types.InlineKeyboardButton("Чт", callback_data="day:четверг"),
        types.InlineKeyboardButton("Пт", callback_data="day:пятница"),
        types.InlineKeyboardButton("Сб", callback_data="day:суббота"),
    )
    return markup


def _send_or_edit_schedule(chat_id, text, message_id=None):
    """
    Отправляет или редактирует сообщение расписания.
    Если есть недавнее сообщение (< TTL) — редактирует, иначе — отправляет новое.
    """
    global schedule_message_state
    now = time.time()
    state = schedule_message_state.get(chat_id)
    ttl = SCHEDULE_MESSAGE_TTL_SECONDS

    markup = get_schedule_days_inline_markup()

    # Редактируем, если есть недавнее сообщение
    if state and (now - state["sent_at"]) < ttl and state.get("message_id"):
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=state["message_id"],
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            schedule_message_state[chat_id] = {"message_id": state["message_id"], "sent_at": now}
            logger.debug("[schedule] отредактировано сообщение chat_id=%s", chat_id)
            return
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str or "message text is not modified" in err_str:
                schedule_message_state[chat_id] = {"message_id": state["message_id"], "sent_at": now}
                logger.debug("[schedule] контент не изменился, редактирование не требуется chat_id=%s", chat_id)
                return
            logger.warning("[schedule] не удалось отредактировать: %s", e)
            # Fallback — отправим новое

    # Отправляем новое сообщение
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    schedule_message_state[chat_id] = {"message_id": msg.message_id, "sent_at": now}
    logger.debug("[schedule] отправлено новое сообщение chat_id=%s", chat_id)


# --- Общая клавиатура главного меню ---
def get_main_menu_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Изменения в расписании"))
    markup.add(types.KeyboardButton("📅 Расписание по дням"))
    return markup


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
    logger.debug("[admin_commands] chat_id=%s text=%r", message.chat.id, message.text)

    # ВАЖНО: /start перехватывался этим хэндлером и игнорировался (не было ветки для него).
    # Теперь передаём /start в start().
    if message.text and message.text.strip().lower() in ("/start", "start"):
        logger.info("[admin_commands] делегируем /start в start() для chat_id=%s", message.chat.id)
        start(message)
        return

    if message.text == "📊 Список пользователей":
        if not users:
            bot.send_message(message.chat.id, "👥 Список пользователей пуст.")
        else:
            text = "\n".join(str(uid) for uid in users)
            bot.send_message(message.chat.id, f"👥 Пользователи ({len(users)}):\n{text}")

    elif message.text == "🔄 Обновить кэш":
        update_cache(force_schedule_refresh=True)
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

    elif message.text == "📅 Расписание по дням":
        _send_or_edit_schedule(message.chat.id, "Выберите день недели, чтобы посмотреть расписание:")

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
        bot.send_message(message.chat.id, "↩️ Возврат в обычное меню.", reply_markup=get_main_menu_markup())
    else:
        # Сообщение от админа не распознано — передаём в handle_text (например, дни недели)
        logger.debug("[admin_commands] не админ-кнопка, передаём в handle_text: %r", message.text)
        handle_text(message)



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
            data = json.load(f)
        users = set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        users = set()


def save_cache_to_disk():
    payload = {
        "date": cache.get("date"),
        "replacements": cache.get("replacements"),
        "replacements_map": cache.get("replacements_map", {}),
        "schedule_by_day": cache.get("schedule_by_day", {}),
        "day_messages": cache.get("day_messages", {}),
        "last_cache_update": cache.get("last_cache_update"),
    }
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_cache_from_disk():
    if not os.path.exists(CACHE_FILE_PATH):
        logger.info("Файл кэша не найден: %s", CACHE_FILE_PATH)
        return
    try:
        with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)

        cache["date"] = payload.get("date", cache["date"])
        cache["replacements"] = payload.get("replacements", cache["replacements"])
        cache["replacements_map"] = payload.get("replacements_map", {})
        cache["schedule_by_day"] = payload.get("schedule_by_day", {})
        # day_messages не загружаем — формат мог измениться, они пересоберутся из schedule_by_day
        cache["day_messages"] = {}
        cache["last_cache_update"] = payload.get("last_cache_update")
        logger.info("Кэш загружен с диска: %s", CACHE_FILE_PATH)
    except Exception as e:
        logger.error("Не удалось загрузить кэш с диска: %s", e, exc_info=True)


def build_day_message_cache():
    day_messages = {}
    for day_key in WEEK_DAYS:
        day_messages[day_key] = build_day_schedule_message(day_key, persist=False)
    cache["day_messages"] = day_messages


# --- Загрузка и парсинг страницы ---
def fetch_page():
    logger.info("Загрузка страницы замен...")
    req = requests.get('https://mpt.ru/izmeneniya-v-raspisanii/', timeout=20).text
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


# Известные корпуса/локации (для строки "Где пара: ...")
KNOWN_LOCATIONS = {"нежинская", "нахимовский"}

# Аббревиатуры длинных дисциплин (короткие — физкультура, иностранный язык — без аббревиатур)
DISCIPLINE_ABBREVIATIONS = {
    "безопасность компьютерных сетей": "БКС",
    "настройка программного обеспечения сетевых устройств": "НПОСУ",
    "организация, принципы построения и функционирования кс": "ОППиФКС",
    "эксплуатация сетевой инфраструктуры": "ЭСИ",
    "администрирование сетевых операционных систем": "АСОС",
}
# Дисциплины, для которых аббревиатуры не добавляем (lowercase для сравнения)
DISCIPLINE_NO_ABBREV = {"физическая культура", "иностранный язык", "оператор связи"}


def _subject_with_abbrev(subject):
    """Добавляет аббревиатуру к длинной дисциплине, если нужно."""
    if not subject or not subject.strip():
        return subject
    s = subject.strip()
    low = s.lower()
    # Пропускаем короткие (физкультура, иностранный язык)
    for excl in DISCIPLINE_NO_ABBREV:
        if excl in low:
            return s
    # Ищем совпадение в словаре аббревиатур
    for full_name, abbrev in DISCIPLINE_ABBREVIATIONS.items():
        if full_name in low:
            return f"{s} ({abbrev})"
    return s


def parse_lessons_list(lessons):
    """
    Разбирает сырой список строк расписания на день.
    Возвращает (location, pairs), где:
    - location: строка "Нежинская", "Нахимовский" или None
    - pairs: dict {1: (subject, teacher), 2: (...), ...}, пустые слоты не заполнены
    """
    location = None
    pairs = {}
    i = 0
    headers = {"пара", "предмет", "преподаватель"}

    while i < len(lessons):
        item = lessons[i].strip()
        low = item.lower()

        # Пропускаем заголовки таблицы
        if low in headers:
            i += 1
            continue

        # Проверяем, является ли элемент локацией (до начала нумерации пар)
        if low in KNOWN_LOCATIONS and not pairs and not re.match(r"^\d+$", item):
            location = item
            i += 1
            continue

        # Ищем блок "номер пары" -> "предмет" -> "преподаватель"
        if re.match(r"^[1-8]$", item):
            pair_num = int(item)
            subject = lessons[i + 1].strip() if i + 1 < len(lessons) else "—"
            teacher = lessons[i + 2].strip() if i + 2 < len(lessons) else "—"
            if re.match(r"^[1-8]$", subject):
                subject, teacher = "—", "—"
            elif re.match(r"^[1-8]$", teacher):
                teacher = "—"
            pairs[pair_num] = (subject, teacher)
            i += 3
            continue

        i += 1

    return location, pairs


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


def load_group_schedule(group_query=GROUP_QUERY, force_refresh=False):
    """Загружает расписание выбранной группы и возвращает словарь по дням."""
    current_schedule = cache.get("schedule_by_day") or {}
    if not force_refresh and current_schedule and cache.get("last_cache_update"):
        age = time.time() - cache["last_cache_update"]
        if age < SCHEDULE_CACHE_TTL_SECONDS:
            logger.info("Используем кэш расписания (возраст %.1f сек)", age)
            return deepcopy(current_schedule)

    logger.info("Загружаем свежее расписание группы: %s", group_query)
    client = MptScheduleClient(timeout=20)
    soup = client.fetch_page()
    targets = client.collect_anchors(soup)
    group = client.find_target(targets, group_query)
    if not group:
        logger.warning("Группа не найдена в расписании: %s", group_query)
        return {day: [] for day in WEEK_DAYS}

    section_text = client.extract_section_text(soup, group.anchor_id)
    if not section_text:
        logger.warning("Не удалось извлечь блок расписания для группы: %s", group_query)
        return {day: [] for day in WEEK_DAYS}

    return parse_schedule_by_day(section_text)


def build_day_schedule_message(day_key, persist=True):
    if persist:
        cached_message = cache.get("day_messages", {}).get(day_key)
        if cached_message:
            logger.info("Отдаём сообщение дня '%s' из кэша", day_key)
            return cached_message

    day_title = WEEK_DAYS[day_key]
    lessons = cache.get("schedule_by_day", {}).get(day_key, [])
    replacements_map = cache.get("replacements_map", {})

    if not lessons:
        message = f"📅 {day_title}\n\nВ этот день пар нет, выходной 🎉"
        if persist:
            cache.setdefault("day_messages", {})[day_key] = message
        return message

    location, pairs = parse_lessons_list(lessons)

    # Если день полностью из практик — показываем короткое сообщение
    if pairs and all(s and s.strip().upper() == "ПРАКТИКА" for s, _ in pairs.values()):
        message = f"📅 {day_title}\n\nПРАКТИКА | У кого то сегодня практика а у кого то выходной"
        if persist:
            cache.setdefault("day_messages", {})[day_key] = message
        return message

    max_pair = max(pairs.keys()) if pairs else 0
    lines = [f"📅 {day_title}"]
    if location:
        lines.append(f"\n📍 Где пара: {location}")
    lines.append("")

    replacement_day = _get_replacement_day()
    for n in range(1, max_pair + 1):
        if n in pairs:
            subject, teacher = pairs[n]
            subject = _subject_with_abbrev(subject or "—")
            subj_escaped = html.escape(subject)
            teach_escaped = html.escape(teacher or "—")

            if (
                replacement_day == day_key
                and str(n) in replacements_map
                and replacements_map[str(n)]
            ):
                # Замена: зачёркнутая пара → новая пара
                repl_str = replacements_map[str(n)]
                parts = repl_str.split(" → ", 1)
                new_subject = _subject_with_abbrev(parts[1].strip()) if len(parts) > 1 else ""
                new_subj_escaped = html.escape(new_subject) if new_subject else html.escape(repl_str)
                line = (
                    f"{n}. <s><b>{subj_escaped}</b> — <i>{teach_escaped}</i></s> "
                    f"→ <b>{new_subj_escaped}</b>"
                )
            else:
                line = f"{n}. <b>{subj_escaped}</b> — <i>{teach_escaped}</i>"
        else:
            line = f"{n}. —"
        lines.append(line)

    message = "\n".join(lines)
    if persist:
        cache.setdefault("day_messages", {})[day_key] = message
    return message


def parsing_dates(soup):
    """Возвращаем дату изменений"""
    date_tag = soup.find('h4')
    return date_tag.text.strip() if date_tag else "Дата не найдена"


def _get_replacement_day():
    """Извлекает день недели замен из cache['date'] (напр. 'вторник' из 'Замены на 03.03.2026 (Вторник)')."""
    date_str = cache.get("date") or ""
    for day_key, day_title in WEEK_DAYS.items():
        if day_title.lower() in date_str.lower() or day_key in date_str.lower():
            return day_key
    return None


# --- Обновление кэша ---
def update_cache(force_schedule_refresh=True):
    global cache
    started_at = time.perf_counter()
    logger.info("Запуск update_cache(force_schedule_refresh=%s)", force_schedule_refresh)

    try:
        t0 = time.perf_counter()
        soup = fetch_page()
        logger.info("Страница замен загружена за %.2f сек", time.perf_counter() - t0)

        t1 = time.perf_counter()
        cache["date"] = parsing_dates(soup)
        cache["replacements"] = get_replacements(soup)
        cache["replacements_map"] = get_replacements_map(soup)
        logger.info(
            "Замены обработаны за %.2f сек (записей: %d)",
            time.perf_counter() - t1,
            len(cache.get("replacements_map", {})),
        )
    except Exception as e:
        logger.error("Ошибка обновления раздела замен: %s", e, exc_info=True)

    try:
        t2 = time.perf_counter()
        cache["schedule_by_day"] = load_group_schedule(force_refresh=force_schedule_refresh)
        logger.info(
            "Расписание по дням обработано за %.2f сек",
            time.perf_counter() - t2,
        )
    except Exception as e:
        logger.error("Ошибка обновления раздела расписания: %s", e, exc_info=True)

    cache["last_cache_update"] = time.time()
    build_day_message_cache()

    try:
        save_cache_to_disk()
        logger.info("Кэш сохранён на диск: %s", CACHE_FILE_PATH)
    except Exception as e:
        logger.error("Ошибка сохранения кэша на диск: %s", e, exc_info=True)

    logger.info("update_cache завершён за %.2f сек", time.perf_counter() - started_at)


# --- Хэндлеры бота ---
@bot.message_handler(commands=['start'])
def start(message):
    logger.debug("[start] chat_id=%s", message.chat.id)
    user_id = message.from_user.id if message.from_user else message.chat.id
    if _check_rate_limit(user_id, message.chat.id, message.from_user):
        return
    try:
        users.add(message.chat.id)
        save_users()
        markup = get_main_menu_markup()
        bot.send_message(
            message.chat.id,
            text=f"Привет, {(message.from_user.first_name if message.from_user else 'друг')}! "
                 f"Я бот, который покажет тебе расписание и изменения в нём.",
            reply_markup=markup
        )
        logger.info("[start] успешно отправлено приветствие chat_id=%s", message.chat.id)
    except Exception as e:
        logger.error("[start] ошибка chat_id=%s: %s", message.chat.id, e, exc_info=True)
        raise


@bot.message_handler(content_types=['text'])
def handle_text(message):
    started_at = time.perf_counter()
    logger.debug("[handle_text] chat_id=%s text=%r", message.chat.id, message.text)
    user_id = message.from_user.id if message.from_user else message.chat.id
    if _check_rate_limit(user_id, message.chat.id, message.from_user):
        return
    if message.text == "Изменения в расписании":
        # Всегда прикладываем главное меню, чтобы кнопки были даже если /start не отработал
        bot.send_message(
            message.chat.id,
            f"{cache['date']}\n{cache['replacements']}",
            reply_markup=get_main_menu_markup(),
        )
        logger.info(
            "[handle_text] ответ 'Изменения в расписании' chat_id=%s за %.3f сек",
            message.chat.id,
            time.perf_counter() - started_at,
        )
    elif message.text == "📅 Расписание по дням":
        _send_or_edit_schedule(message.chat.id, "Выберите день недели, чтобы посмотреть расписание:")
        logger.info("Показали inline-кнопки дней chat_id=%s за %.3f сек", message.chat.id, time.perf_counter() - started_at)
    else:
        logger.debug("[handle_text] неизвестный текст, пропускаем: %r", message.text)


# --- Обработка inline-кнопок выбора дня ---
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("day:"))
def on_day_callback(call):
    """Обработка нажатия inline-кнопки дня: редактируем сообщение вместо отправки нового."""
    user_id = call.from_user.id if call.from_user else call.message.chat.id
    if _check_rate_limit(user_id, call.message.chat.id, call.from_user):
        bot.answer_callback_query(call.id)
        return
    day_key = call.data.replace("day:", "").strip()
    if day_key not in WEEK_DAYS:
        bot.answer_callback_query(call.id, text="Неизвестный день")
        return

    text = build_day_schedule_message(day_key)
    markup = get_schedule_days_inline_markup()

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        schedule_message_state[call.message.chat.id] = {
            "message_id": call.message.message_id,
            "sent_at": time.time(),
        }
        logger.debug("[schedule] отредактировано по callback chat_id=%s день=%s", call.message.chat.id, day_key)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str or "message text is not modified" in err_str:
            schedule_message_state[call.message.chat.id] = {
                "message_id": call.message.message_id,
                "sent_at": time.time(),
            }
            logger.debug("[schedule] контент не изменился по callback chat_id=%s день=%s", call.message.chat.id, day_key)
        else:
            logger.warning("[schedule] ошибка редактирования по callback: %s", e)
            msg = bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")
            schedule_message_state[call.message.chat.id] = {
                "message_id": msg.message_id,
                "sent_at": time.time(),
            }
    bot.answer_callback_query(call.id)


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

# Загружаем кэш с диска сразу (быстро), а обновление в сеть — в фоне,
# чтобы не блокировать запуск polling. Раньше update_cache() блокировал
# до 30+ секунд и бот не отвечал на /start.
load_cache_from_disk()
logger.info("Запуск update_cache в фоновом потоке (не блокируем старт)...")
def _run_initial_cache_update():
    try:
        update_cache(force_schedule_refresh=True)
        logger.info("Фоновое обновление кэша завершено.")
    except Exception as e:
        logger.error("Ошибка фонового обновления кэша при старте: %s", e, exc_info=True)

_cache_thread = threading.Thread(target=_run_initial_cache_update, daemon=True)
_cache_thread.start()

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
    try:
        bot.set_update_listener(debug_updates_listener)
    except AttributeError:
        logger.warning("set_update_listener недоступен в этой версии pyTelegramBotAPI")
    delay = 5
    max_delay = 300
    logger.info("Polling запущен, бот готов принимать сообщения.")
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
