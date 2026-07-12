import os
import time
import threading
import telebot
from telebot import types
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

SIZE = 1000
ENTRY_Z = 2.0
STOP_Z = 3.5
TARGET_Z = 0.5
MIN_CORR = 0.88
MIN_PROFIT = 20
MAX_HALFLIFE = 60   # макс полужизнь в часах (баров), выше - вялая, не берём
MAX_COINS = 60
FEE = 0.055

tracked = {}
users = set()

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти пары")
    kb.row("📋 Мои пары", "🗑 Очистить")
    return kb

def get_closes(symbol, limit=200):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit={limit}"
    r = requests.get(url, timeout=20).json()
    if not isinstance(r, list) or len(r) < 40:
        raise Exception("нет данных " + symbol)
    return [float(c[4]) for c in r]

def half_life(series):
    s = np.array(series)
    if len(s) < 20:
        return None
    lag = s[:-1]
    delta = np.diff(s)
    ml = lag.mean()
    denom = np.sum((lag - ml) ** 2)
    if denom == 0:
        return None
    beta = np.sum((lag - ml) * (delta - delta.mean())) / denom
    if beta >= 0:
        return None
    hl = -np.log(2) / np.log(1 + beta)
    if hl <= 0 or not np.isfinite(hl):
        return None
    return hl

def analyze_pair(ca, cb, window=100):
    n = min(len(ca), len(cb))
    x, y = np.array(ca[-n:]), np.array(cb[-n:])
    corr = np.corrcoef(np.diff(np.log(x)), np.diff(np.log(y)))[0, 1]
    ratio = x / y
    w = ratio[-window:]
    mean, std = w.mean(), w.std()
    if std == 0:
        return None
    z = (ratio[-1] - mean) / std
    dev = abs(ratio[-1] / mean - 1) * 100
    hl = half_life(w)
    return corr, z, dev, hl

def profit_stop(dev, z):
    profit = SIZE * dev / 100 - SIZE * FEE / 100 * 4
    # ход до стопа в % от текущего z
    extra = (STOP_Z - abs(z))
    loss_pct = dev / max(abs(z), 0.01) * extra
    loss = SIZE * loss_pct / 100 + SIZE * FEE / 100 * 4
    rr = profit / loss if loss > 0 else 0
    return profit, loss, rr

def card(a, b, corr, z, dev, hl):
    profit, loss, rr = profit_stop(dev, z)
    short = a if z > 0 else b
    long = b if z > 0 else a
    c_ok = "✅" if corr >= 0.90 else "⚠️"
    h_ok = "✅" if (hl and hl <= 30) else ("⚠️" if hl else "❌")
    z_ok = "✅"
    hl_txt = (str(round(hl)) + "ч") if hl else "нет возврата"
    rr_ok = "✅" if rr >= 1.5 else "⚠️"
    txt = (
        "🔗 " + a + " / " + b + "\n\n"
        + c_ok + " Корреляция: " + str(round(corr*100)) + "%\n"
        + h_ok + " Полужизнь: " + hl_txt + "\n"
        + z_ok + " z-score: " + format(z, "+.2f") + " (зона входа)\n\n"
        "📊 Шорт " + short + " / Лонг " + long + "\n"
        "💰 Профит (z→0): ~$" + str(round(profit)) + "\n"
        "🛑 Стоп (z=" + str(STOP_Z) + "): ~$" + str(round(loss)) + "\n"
        + rr_ok + " R:R: " + str(round(rr, 1)) + "\n\n"
        "⚙️ на $" + str(SIZE) + "/ногу, плечо 1х!"
    )
    return txt

def top_coins():
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    r = requests.get(url, timeout=25).json()
    usdt = [x for x in r if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"].replace("USDT", "") for x in usdt[:MAX_COINS]]

@bot.message_handler(commands=['start'])
def start(m):
    users.add(m.chat.id)
    bot.send_message(m.chat.id, "Привет! Слежу за парами автоматически.\nЖми кнопки 👇\n\nПришлю хорошую пару когда появится, и алерт на выход/стоп по отслеживаемым.", reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🔍 Найти пары")
def btn_scan(m):
    users.add(m.chat.id)
    bot.send_message(m.chat.id, "Ищу лучшие пары (корр, полужизнь, профит)... жди 2-4 мин")
    do_scan(m.chat.id, manual=True)

@bot.message_handler(func=lambda m: m.text == "📋 Мои пары")
def btn_list(m):
    t = tracked.get(m.chat.id, [])
    if not t:
        bot.send_message(m.chat.id, "Ничего не отслеживается.", reply_markup=menu())
        return
    out = "Отслеживаю:\n\n"
    for a, b in list(t):
        try:
            ca, cb = get_closes(a+"USDT"), get_closes(b+"USDT")
            corr, z, dev, hl = analyze_pair(ca, cb)
            if abs(z) <= TARGET_Z:
                st = "🎯 СОШЛИСЬ - выходи в профит"
            elif abs(z) >= STOP_Z:
                st = "🛑 СТОП - выходи"
            else:
                st = "⏳ держим (z=" + format(z, "+.2f") + ")"
            out += a + "/" + b + ": " + st + "\n"
        except:
            out += a + "/" + b + ": ошибка данных\n"
    bot.send_message(m.chat.id, out, reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🗑 Очистить")
def btn_clear(m):
    tracked[m.chat.id] = []
    bot.send_message(m.chat.id, "Всё снято.", reply_markup=menu())

@bot.callback_query_handler(func=lambda c: c.data.startswith("track:"))
def cb_track(c):
    _, a, b = c.data.split(":")
    tracked.setdefault(c.message.chat.id, [])
    if (a, b) not in tracked[c.message.chat.id]:
        tracked[c.message.chat.id].append((a, b))
    bot.answer_callback_query(c.id, "Отслеживаю " + a + "/" + b)
    bot.send_message(c.message.chat.id, "✅ " + a + "/" + b + " добавлена. Пришлю алерт на выход/стоп.", reply_markup=menu())

def do_scan(chat_id, manual=False):
    try:
        coins = top_coins()
        closes = {}
        for c in coins:
            try:
                closes[c] = get_closes(c + "USDT")
            except:
                pass
            time.sleep(0.1)
        found = []
        cl = list(closes.keys())
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                a, b = cl[i], cl[j]
                try:
                    res = analyze_pair(closes[a], closes[b])
                    if not res:
                        continue
                    corr, z, dev, hl = res
                    if corr < MIN_CORR:
                        continue
                    if abs(z) < ENTRY_Z or abs(z) >= STOP_Z:
                        continue
                    if hl is None or hl > MAX_HALFLIFE:
                        continue
                    profit, loss, rr = profit_stop(dev, z)
                    if profit < MIN_PROFIT:
                        continue
                    found.append((abs(z), a, b, corr, z, dev, hl))
                except:
                    pass
        if not found:
            if manual:
                bot.send_message(chat_id, "Хороших пар сейчас нет (корр≥88%, полужизнь короткая, профит≥$20). Загляну позже сам.", reply_markup=menu())
            return
        found.sort(reverse=True)
        bot.send_message(chat_id, "Нашёл " + str(len(found)) + " пар(ы):")
        for az, a, b, corr, z, dev, hl in found[:8]:
            txt = card(a, b, corr, z, dev, hl)
            ikb = types.InlineKeyboardMarkup()
            ikb.add(types.InlineKeyboardButton("➡️ Отслеживать " + a + "/" + b, callback_data="track:" + a + ":" + b))
            bot.send_message(chat_id, txt, reply_markup=ikb)
    except Exception:
        if manual:
            bot.send_message(chat_id, "Ошибка при поиске, попробуй ещё раз.", reply_markup=menu())

def auto_loop():
    last_scan = 0
    while True:
        try:
            for chat_id, pairs in list(tracked.items()):
                for a, b in list(pairs):
                    try:
                        ca, cb = get_closes(a+"USDT"), get_closes(b+"USDT")
                        corr, z, dev, hl = analyze_pair(ca, cb)
                        if abs(z) <= TARGET_Z:
                            bot.send_message(chat_id, "🎯 ВЫХОД В ПРОФИТ: " + a + "/" + b + " сошлись (z=" + format(z, "+.2f") + "). Закрывай в плюс!", reply_markup=menu())
                            tracked[chat_id].remove((a, b))
                        elif abs(z) >= STOP_Z:
                            bot.send_message(chat_id, "🛑 СТОП: " + a + "/" + b + " (z=" + format(z, "+.2f") + "). Закрывай, не досиживай!", reply_markup=menu())
                            tracked[chat_id].remove((a, b))
                    except:
                        pass
            if time.time() - last_scan > 1800:
                last_scan = time.time()
                for uid in list(users):
                    try:
                        do_scan(uid, manual=False)
                    except:
                        pass
        except:
            pass
        time.sleep(90)

threading.Thread(target=auto_loop, daemon=True).start()
print("Бот запущен...")
bot.infinity_polling()
