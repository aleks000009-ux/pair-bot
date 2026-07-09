import os
import telebot

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Я твой парный трекер. Пока умею только здороваться 🙂")

print("Бот запущен...")
bot.infinity_polling()
