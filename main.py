import os
import time
import threading
import telebot
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

SIZE = 1000          # $ на ногу
ENTRY_Z = 2.0        # зона входа
STOP_Z = 3.5         # стоп
TARGET_Z = 0.5       # почти сошлись = выход
MIN_CORR = 0.85      # минимальная корреляция

# список пар для сканера
SCAN_PAIRS = [
    ("BTC","LINK"),("ETH","BNB"),("SOL","AVAX"),("DOGE","SHIB"),
    ("LINK","DOT"),("ADA","XRP"),("AVAX","NEAR"),("ATOM","DOT"),
    ("UNI","AAVE"),("LTC","BCH"),("ETH","SOL"),("BNB","SOL"),
    ("ARB","OP"),("APT","SUI"),("FIL","AR")
]

tracked = {}   # {chat_id: [(a, b), ...]}

def get_closes(symbol, limit=160):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    r = requests.get(url, timeout=20).json()
    if not isinstance(r, list) or len(r) < 30:
        raise Exception(f"нет данных {symbol}")
    return [float(c[4]) for c in r]

def stats(a, b, window=100):
    ca = get_closes(a+"USDT")
    cb = get_closes(b+"USDT")
    n = min(len(ca), len(cb))
    ca, cb = ca[-n:], cb[-n:]
    ra = np.diff(np.log(ca)); rb = np.diff(np.log(cb))
    corr = np.corrcoef(ra, rb)[0, 1]
    ratio = np.array(ca) / np.array(cb)
    w = ratio[-window:]
    mean, std = w.mean(), w.std()
    z = (ratio[-1] - mean) / std
    dev = abs(ratio[-1]/mean - 1) * 100          # отклонение %
    return corr, z, dev

def money(dev, extra_z_to_stop):
    profit = SIZE * dev / 100
    fees = SIZE * 0.055/100 * 4
    loss = SIZE * (extra_z_to_stop) * (dev/max(abs(dev),0.01)) / 100
    return profit - fees

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, "Команды:\n/scan - найти пары для входа\n/track BTC LINK - отслеживать\n/stop - снять отслеживание\n/list - что отслеживается")

@bot.message_handler(commands=['scan'])
def scan(m):
    bot.reply_to(m, "Сканирую пары...")
    found = []
    for a, b in SCAN_PAIRS:
        try:
            corr, z, dev = stats(a, b)
            if corr >= MIN_CORR and abs(z) >= ENTRY_Z and abs(z) < STOP_Z:
                profit = SIZE*dev/100 - SIZE*0.055/100*4
                short = a if z > 0 else b
                long = b if z > 0 else a
                found.append((abs(z), a, b, corr, z, dev, profit, short, long))
        except:
            pass
        time.sleep(0.3)
    if not found:
        bot.send_message(m.chat.id, "Сейчас нет годных пар (корр>=85%, z>=2). Попробуй позже.")
        return
    found.sort(reverse=True)
    txt = "🎯 ПАРЫ ДЛЯ ВХОДА:\n\n"
    for az, a, b, corr, z, dev, profit, short, long in found:
        txt += (f"{a}/{b}\n"
                f"корр {corr*100:.0f}% · z {z:+.2f} · расх {dev:.2f}%\n"
                f"шорт {short} / лонг {long}\n"
                f"макс профит ~${profit:.0f} на $1000/ногу\n"
                f"стоп при z={STOP_Z}\n"
                f"➡️ /track {a} {b}\n\n")
    bot.send_message(m.chat.id, txt)

@bot.message_handler(commands=['track'])
def track(m):
    try:
        p = m.text.split()
        a, b = p[1].upper(), p[2].upper()
        corr, z, dev = stats(a, b)
        tracked.setdefault(m.chat.id, [])
        if (a, b) not in tracked[m.chat.id]:
            tracked[m.chat.id].append((a, b))
        profit = SIZE*dev/100 - SIZE*0.055/100*4
        bot.reply_to(m, f"✅ Отслеживаю {a}/{b}\nz сейчас {z:+.2f}, корр {corr*100:.0f}%\nМакс профит ~${profit:.0f}\nПришлю алерт на выход (z→0) или стоп (z={STOP_Z}).")
    except Exception as e:
        bot.reply_to(m, f"Формат: /track BTC LINK\n({e})")

@bot.message_handler(commands=['stop'])
def stop(m):
    tracked[m.chat.id] = []
    bot.reply_to(m, "Отслеживание снято.")

@bot.message_handler(commands=['list'])
def lst(m):
    t = tracked.get(m.chat.id, [])
    if not t:
        bot.reply_to(m, "Ничего не отслеживается.")
    else:
        bot.reply_to(m, "Отслеживаю:\n" + "\n".join(f"{a}/{b}" for a,b in t))

def monitor():
    while True:
        time.sleep(180)
        for chat_id, pairs in list(tracked.items()):
            for a, b in list(pairs):
                try:
                    corr, z, dev = stats(a, b)
                    if abs(z) <= TARGET_Z:
                        bot.send_message(chat_id, f"🎯 {a}/{b} СОШЛИСЬ! z={z:+.2f}\nВЫХОДИ В ПРОФИТ. Пара вернулась к норме.")
                        tracked[chat_id].remove((a, b))
                    elif abs(z) >= STOP_Z:
                        bot.send_message(chat_id, f"🛑 {a}/{b} СТОП! z={z:+.2f}\nВЫХОДИ ПО СТОПУ. Связь ломается, не досиживай.")
                        tracked[chat_id].remove((a, b))
                except:
                    pass

threading.Thread(target=monitor, daemon=True).start()
print("Бот запущен...")
bot.infinity_polling()
