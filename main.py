import os
import time
import threading
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
_cache = {}

def get_closes(symbol, limit=160):
    if symbol in _cache and time.time() - _cache[symbol][0] < 120:
        return _cache[symbol][1]
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    r = requests.get(url, timeout=20).json()
    if not isinstance(r, list) or len(r) < 30:
        raise Exception(f"нет данных {symbol}")
    closes = [float(c[4]) for c in r]
    _cache[symbol] = (time.time(), closes)
    return closes

def top_coins():
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    r = requests.get(url, timeout=25).json()
    usdt = [x for x in r if x["symbol"].endswith("USDT") and not any(s in x["symbol"] for s in ["UP","DOWN","BULL","BEAR"])]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    coins = [x["symbol"].replace("USDT","") for x in usdt[:MAX_COINS]]
    return coins

def stats_sym(a, b, window=100):
    ca = get_closes(a+"USDT"); cb = get_closes(b+"USDT")
    n = min(len(ca), len(cb))
    ca, cb = ca[-n:], cb[-n:]
    ra = np.diff(np.log(ca)); rb = np.diff(np.log(cb))
    corr = np.corrcoef(ra, rb)[0, 1]
    ratio = np.array(ca) / np.array(cb)
    w = ratio[-window:]
    mean, std = w.mean(), w.std()
    z = (ratio[-1] - mean) / std
    dev = abs(ratio[-1]/mean - 1) * 100
    return corr, z, dev

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, "Команды:\n/scan - найти пары для входа (топ-60 монет)\n/track BTC LINK - отслеживать\n/stop - снять всё\n/list - что отслеживается")

@bot.message_handler(commands=['scan'])
def scan(m):
    bot.reply_to(m, "Сканирую топ-60 монет (~1700 пар). Это займёт 2-4 мин, пришлю результат...")
    try:
        coins = top_coins()
    except Exception as e:
        bot.send_message(m.chat.id, f"Не удалось получить монеты: {e}")
        return
    # грузим свечи один раз на монету
    closes = {}
    for c in coins:
        try:
            closes[c] = get_closes(c+"USDT")
        except:
            pass
        time.sleep(0.15)
    found = []
    cl = list(closes.keys())
    for i in range(len(cl)):
        for j in range(i+1, len(cl)):
            a, b = cl[i], cl[j]
            try:
                ca, cb = closes[a], closes[b]
                n = min(len(ca), len(cb))
                x, y = np.array(ca[-n:]), np.array(cb[-n:])
                ra = np.diff(np.log(x)); rb = np.diff(np.log(y))
                corr = np.corrcoef(ra, rb)[0,1]
                if corr < MIN_CORR:
                    continue
                ratio = x / y
                w = ratio[-100:]
                mean, std = w.mean(), w.std()
                if std == 0:
                    continue
                z = (ratio[-1]-mean)/std
                if abs(z) < ENTRY_Z or abs(z) >= STOP_Z:
                    continue
                dev = abs(ratio[-1]/mean - 1)*100
                profit = SIZE*dev/100 - SIZE*0.055/100*4
                short = a if z > 0 else b
                long = b if z > 0 else a
                found.append((abs(z), a, b, corr, z, dev, profit, short, long))
            except:
                pass
    if not found:
        bot.send_message(m.chat.id, "Годных пар сейчас нет (корр>=85%, z>=2). Попробуй позже.")
        return
    found.sort(reverse=True)
    txt = f"🎯 НАЙДЕНО {len(found)} ПАР ДЛЯ ВХОДА (топ по жирности):\n\n"
    for az, a, b, corr, z, dev, profit, short, long in found[:15]:
        txt += (f"{a}/{b}  z {z:+.2f} · корр {corr*100:.0f}%\n"
                f"шорт {short} / лонг {long} · расх {dev:.2f}%\n"
                f"макс профит ~${profit:.0f} · стоп z={STOP_Z}\n"
                f"➡️ /track {a} {b}\n\n")
