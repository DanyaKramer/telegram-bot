import requests
from bs4 import BeautifulSoup
import telebot
import config
from telebot import types
import logging as loger


loger.basicConfig(filename='Bot_Errs.log', level=loger.ERROR)


sf_sticker_id = 'CAACAgIAAxkBAAIBXWe5tajRZf78MwYVP5P8stp12RZvAALJVwACoWHpS6EShjjI1IcoNgQ'


bot = telebot.TeleBot(config.token)



@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("Изменения в расписании")
    

    markup.add(btn1)
    bot.send_message(message.chat.id, text="Привет, {0.first_name}! Я бот который покажет тебе расписание и измениения в нём.".format(message.from_user), reply_markup=markup)

@bot.message_handler(content_types=['text', 'document', 'audio', 'sticker'])
def get_text_messages(message):
        if message.content_type == 'sticker':
            sticker_id = message.sticker.file_id
            bot.reply_to(message, f"ID стикера: {sticker_id}")

        if message.text == "сосал?":
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



        if message.text == "Изменения в расписании":
            try:
               bot.send_message(message.from_user.id, parsing_lessons())
            except telebot.apihelper.ApiTelegramException as e:
                if "message text is empty" in str(e):
                    bot.send_message(message.from_user.id, "Возможно замен нет")
                 
        elif message.text == "Она че то делает, но не работает":
             bot.send_message(message.from_user.id, "Долбоеб написано же что не работает")



def parsing_lessons():
        
    req = requests.get('https://mpt.ru/izmeneniya-v-raspisanii/').text
    
    soup = BeautifulSoup(req, 'lxml')
    
    target_text = "СА-1-23"

    groups = soup.find_all('div', class_='table-responsive')
    for group in groups:
        table = group.find('table')
        table_text = table.get_text().lower()
        if target_text.lower() in table_text:
            lessons = table.get_text()
            print(lessons)
            return lessons


bot.polling(none_stop=True, interval=0)
