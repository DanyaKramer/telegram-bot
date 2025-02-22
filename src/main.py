import requests
from bs4 import BeautifulSoup
import telebot
import config as config
from telebot import types


bot = telebot.TeleBot(config.token) # в файле config.py содержится API ключ для работы бота


@bot.message_handler(commands=['start']) # приветственное сообщение
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("Изменения в расписании")
    markup.add(btn1)
    bot.send_message(message.chat.id, text="Привет, {0.first_name}! Я бот который покажет тебе расписание и измениения в нём.".format(message.from_user), reply_markup=markup)

@bot.message_handler(content_types=['text', 'document', 'audio', 'sticker']) # контент который бот понимает 
def get_text_messages(message):
        
        if message.content_type == 'sticker': #выводит ID стикера telegram
            sticker_id = message.sticker.file_id
            bot.reply_to(message, f"ID стикера: {sticker_id}")
        
        if message.text == "сосал?": #пасхалко
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            btnYes = types.KeyboardButton("Да!")
            btnNo = types.KeyboardButton("ДА!")
            back = types.KeyboardButton("Обратно к расписанию")
            markup.add(btnYes, btnNo, back)
            bot.send_message(message.from_user.id, "А ты?", reply_markup=markup)
        elif message.text == "Обратно к расписанию":
            start(message)
        elif message.text == "ДА!" or message.text == "Да!": 
            bot.send_sticker(message.from_user.id, sf_sticker_id)
       
        if message.text == "Изменения в расписании": # просмотр замен 
            try:
               bot.send_message(message.from_user.id, f"{parsing_dates()}\n" + f"{parsing_lessons()}")
            except telebot.apihelper.ApiTelegramException as e: # если замен нету, то сообщение будет пустым и в результает выскочит ошибка, чтобы этого избежать используется данная строка
                if "message text is empty" in str(e):
                    bot.send_message(message.from_user.id, "Возможно замен нет")


def parsing_lessons():
        
    req = requests.get('https://mpt.ru/izmeneniya-v-raspisanii/').text #создание запроса на получение html страницы
    soup = BeautifulSoup(req, 'lxml') # парсинг страницы
    target_text = "СА-1-23" # целевая группа для парсинга
    groups = soup.find_all('div', class_='table-responsive') # находит все таблицы 
    result = [] # сюда запишется текст с заменами
    
    for group in groups:
        table = group.find('table') # в найденных div находит table 
        table_text = table.get_text().lower() # запись имени таблицы в переменную
        if target_text.lower() in table_text: # проверка наличия целевой группы в таблице
            rows = table.find_all('tr') # выборка всех строк для дальнейшего форматирования
            for row in rows: # перебор всех строк для выборки всех ячеек
                cells = row.find_all('td') #
                if len(cells) >= 4: # проверка на количество ячеек, оно дол
                    lesson = cells[0].text.strip() # первая найденная ячейка в строке отвечает за номер пары
                    replaced = cells[1].text.strip() # вторая ячейка отвечает за наименование заменяемой пары 
                    replacement = cells[2].text.strip() # третья ячейка отвечает за наименование пары на какую меняют
                    added = cells[3].text.strip() # четвертая ячейка отвечает за дату и время когда была добавлена замена

                    formatted_message = ( # после полученных данных сообщение форматируется для последующей обработки
                        f"Пара: {lesson}\n"
                        f"Что заменяют: {replaced}\n"
                        f"На что заменяют: {replacement}\n"
                        f"Добавлена: {added}\n"
                    ) 
                    result.append(formatted_message) # форматированное сообщение добавляется в список
    if result: # проверка списка на наличие предметов в нём
        return "\n".join(result) # если  в списке имеются предметы, тогда функция возвращает result подсоединяя каждый предмет с новой строки
    else: # в других случаях возвращает сообщение об отсутствии замен 
        return "Возможно замен нет"
        
def parsing_dates():
    req = requests.get('https://mpt.ru/izmeneniya-v-raspisanii/').text #создание запроса на получение html страницы
    
    soup = BeautifulSoup(req, 'lxml') # парсинг страницы
    date = soup.find('h4').text #поиск даты для дальнейшей обработки
    return date
        
        
if __name__ == "__main__":

    # check_lessons = time(18, 0, 0) - в дальнейшем должно быть время для проверки замен 
    sf_sticker_id = 'CAACAgIAAxkBAAIBXWe5tajRZf78MwYVP5P8stp12RZvAALJVwACoWHpS6EShjjI1IcoNgQ' # это просто id стикера для работы пасхалки
    
    bot.polling(none_stop=True, interval=0) # поддержка работоспособности скрипта

