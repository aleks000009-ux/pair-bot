import os
import time
import telebot
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

SIZE = 1000
ENTRY_Z = 2.0
STOP_Z = 3.5
TARGET_Z = 0.5
MIN_CORR = 0.85
MAX_COINS = 60

tracked = {}

def get_closes(symbol, limit=160):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    r = requests.get(url, timeout=20).json()
    if not isinstance(r, list) or len(r) < 30:
        raise Exception("нет данных " + symbol)
    return [float(c[4]) for c in r]

def stats_sym(a, b, window=100):
    ca = get_closes(a + "USDT")
    cb = get_closes(b + "USDT")
    n = min(len(ca), len(cb))
    ca, cb = ca[-n:], cb[-n:]
    ra = np.diff(np.log(ca))
    rb = np.diff(np.log(cb))
    corr = np.corrcoef(ra, rb)[0, 1]
    ratio = np.array(ca) / np.array(cb)
    w = ratio[-window:]
    mean, std = w.mean(), w.std()
    z = (ratio[-1] - mean) / std
    dev = abs(ratio[-1] / mean - 1) * 100
    return corr, z, dev

def top_coins():
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    r = requests.get(url, timeout=25).json()
    usdt = [x for x in r if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"].replace("USDT", "") for x in usdt[:MAX_COINS]]

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, "Команды:\n/scan - найти пары\n/track BTC LINK - отслеживать\n/check - проверить отслеживаемые\n/stop - снять всё\n/list - список")

@bot.message_handler(commands=['scan'])
def scan(m):
    bot.send_message(m.chat.id, "Сканирую топ-60 монет, жди 2-4 мин...")
    try:
        coins = top_coins()
        closes = {}
        for c in coins:
            try:
                closes[c] = get_closes(c + "USDT")
            except:
                pass
            time.sleep(0.15)
        found = []
        cl = list(closes.keys())
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                a, b = cl[i], cl[j]
                try:
                    ca, cb = closes[a], closes[b]
                    n = min(len(ca), len(cb))
                    x, y = np.array(ca[-n:]), np.array(cb[-n:])
                    corr = np.corrcoef(np.diff(np.log(x)), np.diff(np.log(y)))[0, 1]
                    if corr < MIN_CORR:
                        continue
                    ratio = x / y
                    w = ratio[-100:]
                    mean, std = w.mean(), w.std()
                    if std == 0:
                        continue
                    z = (ratio[-1] - mean) / std
                    if abs(z) < ENTRY_Z or abs(z) >= STOP_Z:
                        continue
                    dev = abs(ratio[-1] / mean - 1) * 100
                    profit = SIZE * dev / 100 - SIZE * 0.055 / 100 * 4
                    short = a if z > 0 else b
                    long = b if z > 0 else a
                    found.append((abs(z), a, b, corr, z, dev, profit, short, long))
                except:
                    pass
        if not found:
            bot.send_message(m.chat.id, "Годных пар сейчас нет (корр>=85%, z>=2).")
            return
        found.sort(reverse=True)
        txt = "НАЙДЕНО " + str(len(found)) + " ПАР:\n\n"
        for az, a, b, corr, z, dev, profit, short, long in found[:15]:
            txt += (a + "/" + b + "  z " + format(z, "+.2f") + " корр " + str(round(corr*100)) + "%\n"
                    "шорт " + short + " / лонг " + long + " расх " + str(round(dev, 2)) + "%\n"
                    "макс профит ~$" + str(round(profit)) + " стоп z=" + str(STOP_Z) + "\n"
                    "/track " + a + " " + b + "\n\n")
        bot.send_message(m.chat.id, txt)
    except Exception as e:
        bot.send_message(m.chat.id, "Ошибка скана: " + str(e))

@bot.message_handler(commands=['track'])
def track(m):
    try:
        p = m.text.split()
        a, b = p[1].upper(), p[2].upper()
        corr, z, dev = stats_sym(a, b)
        tracked.setdefault(m.chat.id, [])
        if (a, b) not in tracked[m.chat.id]:
            tracked[m.chat.id].append((a, b))
        profit = SIZE * dev / 100 - SIZE * 0.055 / 100 * 4
        warn = "" if corr >= MIN_CORR else "\nВНИМАНИЕ: корреляция ниже 85%, риск!"
        bot.reply_to(m, "Отслеживаю " + a + "/" + b + "\nz " + format(z, "+.2f") + " корр " + str(round(corr*100)) + "%\nмакс профит ~$" + str(round(profit)) + "\nПиши /check чтобы проверить." + warn)
    except Exception as e:
        bot.reply_to(m, "Формат: /track BTC LINK (" + str(e) + ")")

@bot.message_handler(commands=['check'])
def check(m):
    t = tracked.get(m.chat.id, [])
    if not t:
        bot.reply_to(m, "Ничего не отслеживается. Добавь: /track BTC LINK")
        return
    out = ""
    for a, b in list(t):
        try:
            corr, z, dev = stats_sym(a, b)
            if abs(z) <= TARGET_Z:
                st = "СОШЛИСЬ - выходи в профит"
            elif abs(z) >= STOP_Z:
                st = "СТОП - выходи по стопу"
            else:
                st = "держим, ждём"
            out += a + "/" + b + ": z " + format(z, "+.2f") + " - " + st + "\n"
        except:
            out += a + "/" + b + ": ошибка данных\n"
    bot.reply_to(m, out)

@bot.message_handler(commands=['stop'])
def stop(m):
    tracked[m.chat.id] = []
    bot.reply_to(m, "Отслеживание снято.")

@bot.message_handler(commands=['list'])
def lst(m):
    t = tracked.get(m.chat.id, [])
    if t:
        bot.reply_to(m, "Отслеживаю:\n" + "\n".join(a + "/" + b for a, b in t))
    else:
        bot.reply_to(m, "Ничего не отслеживается.")

print("Бот запущен...")
bot.infinity_polling()
