import os
import telebot
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

def get_closes(symbol, limit=160):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    resp = requests.get(url, timeout=20)
    r = resp.json()
    if not isinstance(r, list) or len(r) < 30:
        raise Exception(f"код {resp.status_code}: {resp.text[:150]}")
    closes = [float(c[4]) for c in r]
    return closes

def analyze(a, b, window=100):
    ca = get_closes(a)
    cb = get_closes(b)
    n = min(len(ca), len(cb))
    ca, cb = ca[-n:], cb[-n:]
    ra = np.diff(np.log(ca))
    rb = np.diff(np.log(cb))
    corr = np.corrcoef(ra, rb)[0, 1]
    ratio = np.array(ca) / np.array(cb)
    w = ratio[-window:]
    z = (ratio[-1] - w.mean()) / w.std()
    return corr, z

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Напиши: /pair BTC LINK")

@bot.message_handler(commands=['pair'])
def pair(message):
    try:
        parts = message.text.split()
        a = parts[1].upper() + "USDT"
        b = parts[2].upper() + "USDT"
        bot.reply_to(message, f"Считаю {parts[1].upper()}/{parts[2].upper()}...")
        corr, z = analyze(a, b)
        if abs(z) >= 3.5:
            state = "СТОП - расхождение слишком большое"
        elif abs(z) >= 2:
            state = "ЗОНА ВХОДА"
        elif abs(z) <= 0.5:
            state = "почти сошлись (выход)"
        else:
            state = "ждём"
        short = a if z > 0 else b
        long = b if z > 0 else a
        text = (
            f"{parts[1].upper()}/{parts[2].upper()}\n\n"
            f"Корреляция: {corr*100:.0f}%\n"
            f"z-score: {z:+.2f}\n"
            f"Статус: {state}\n\n"
            f"шорт {short.replace('USDT','')}\n"
            f"лонг {long.replace('USDT','')}"
        )
        bot.reply_to(message, text)
    except Exception as e:
        bot.reply_to(message, f"Ошибка. Формат: /pair BTC LINK\n({e})")

print("Бот запущен...")
bot.infinity_polling()
