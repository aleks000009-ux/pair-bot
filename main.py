import os
import json
import time
import threading
import telebot
from telebot import types
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

SIZE = 1000
RSI_LEVEL = 40
VOL_MULT = 1.1
MAX_DIST = 0.8
MAX_RISK = 3.0
RR = 3
ATR_BUF = 0.5
MAX_COINS = 200
FEE = 0.055
DB = "trades.json"

tracked = {}
users = set()
sent_signals = {}
history = {}   # {chat_id: [ {sym, side, result, pnl, entry, exit} ]}

def save():
    try:
        with open(DB, "w") as f:
            json.dump({"tracked": {str(k): v for k, v in tracked.items()},
                       "users": list(users),
                       "history": {str(k): v for k, v in history.items()}}, f)
    except Exception as e:
        print("save error:", e)

def load():
    global tracked, users, history
    try:
        with open(DB) as f:
            d = json.load(f)
        tracked = {int(k): v for k, v in d.get("tracked", {}).items()}
        users = set(d.get("users", []))
        history = {int(k): v for k, v in d.get("history", {}).items()}
        print("загружено: сделок", sum(len(v) for v in tracked.values()), "| история", sum(len(v) for v in history.values()))
    except Exception:
        print("нет сохранённых данных, старт с нуля")

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти отскоки")
    kb.row("📋 Мои сделки", "📊 Статистика")
    kb.row("🗑 Очистить")
    return kb

def get_klines(symbol, limit=250):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=4h&limit={limit}"
    r = requests.get(url, timeout=20).json()
    if not isinstance(r, list) or len(r) < 3:
        raise Exception("нет данных " + symbol)
    return [{"o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
             "c": float(c[4]), "v": float(c[5]), "close_t": int(c[6])} for c in r]

def get_price(symbol):
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
    r = requests.get(url, timeout=15).json()
    if "price" not in r:
        raise Exception("нет цены " + symbol)
    return float(r["price"])

def rsi(closes, period=14):
    c = np.array(closes)
    d = np.diff(c)
    if len(d) < period:
        return None
    gain = np.where(d > 0, d, 0)
    loss = np.where(d < 0, -d, 0)
    ag = gain[:period].mean()
    al = loss[:period].mean()
    for i in range(period, len(d)):
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))

def atr(kl, period=14):
    if len(kl) < period + 1:
        return None
    trs = []
    for i in range(1, len(kl)):
        h, l, pc = kl[i]["h"], kl[i]["l"], kl[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(trs[-period:]))

def ema(closes, period):
    c = np.array(closes, dtype=float)
    if len(c) < period:
        return None
    k = 2 / (period + 1)
    e = c[:period].mean()
    for p in c[period:]:
        e = p * k + e * (1 - k)
    return float(e)

def find_levels(kl, kind):
    lv = []
    for i in range(3, len(kl) - 3):
        if kind == "sup":
            v = kl[i]["l"]
            if v < kl[i-1]["l"] and v < kl[i-2]["l"] and v < kl[i+1]["l"] and v < kl[i+2]["l"]:
                lv.append(v)
        else:
            v = kl[i]["h"]
            if v > kl[i-1]["h"] and v > kl[i-2]["h"] and v > kl[i+1]["h"] and v > kl[i+2]["h"]:
                lv.append(v)
    return lv

def detect_reversal(kl, side):
    if len(kl) < 2:
        return None
    last, prev = kl[-1], kl[-2]
    body = abs(last["c"] - last["o"])
    rng = last["h"] - last["l"]
    if rng <= 0:
        return None
    upper = last["h"] - max(last["o"], last["c"])
    lower = min(last["o"], last["c"]) - last["l"]
    small = body <= rng * 0.4
    if side == "long":
        if lower >= body * 2 and small and upper <= body * 0.8 and lower >= rng * 0.5:
            return "🔨 Пин-бар"
        if prev["c"] < prev["o"] and last["c"] > last["o"] and last["c"] >= prev["o"] and last["o"] <= prev["c"]:
            return "🫸 Поглощение"
    else:
        if upper >= body * 2 and small and lower <= body * 0.8 and upper >= rng * 0.5:
            return "⭐ Пин-бар"
        if prev["c"] > prev["o"] and last["c"] < last["o"] and last["o"] >= prev["c"] and last["c"] <= prev["o"]:
            return "🫷 Поглощение"
    return None

def top_coins():
    url = "https://data-api.binance.vision/api/v3/ticker/24hr"
    r = requests.get(url, timeout=25).json()
    usdt = [x for x in r if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"].replace("USDT", "") for x in usdt[:MAX_COINS]]

def analyze(sym):
    raw = get_klines(sym + "USDT")
    if len(raw) < 60:
        return None
    now_ms = int(time.time() * 1000)
    kl = raw[:-1] if raw[-1]["close_t"] > now_ms else raw
    if len(kl) < 60:
        return None
    closes = [k["c"] for k in kl]
    last = kl[-1]
    price = get_price(sym + "USDT")
    v20 = np.mean([k["v"] for k in kl[-21:-1]])
    if v20 <= 0:
        return None
    vr = last["v"] / v20
    if vr < VOL_MULT:
        return None
    r = rsi(closes)
    if r is None:
        return None
    a = atr(kl)
    if not a:
        return None
    e200 = ema(closes, min(200, len(closes)))
    rel = (price - e200) / e200 * 100 if e200 else 0
    out = []
    if r < RSI_LEVEL and rel >= -15:
        sups = [s for s in find_levels(kl, "sup") if s <= price * 1.002]
        if sups:
            lvl = min(sups, key=lambda s: abs(price - s))
            dist = abs(price - lvl) / lvl * 100
            if dist <= MAX_DIST and price >= lvl * 0.995:
                sl = lvl - a * ATR_BUF
                if sl < price:
                    risk = price - sl
                    rp = risk / price * 100
                    if 0 < rp <= MAX_RISK:
                        pat = detect_reversal(kl, "long") or "нет разворотной"
                        out.append({"side": "long", "sym": sym, "entry": price, "lvl": lvl,
                                    "sl": sl, "tp": price + risk * RR, "risk_pct": rp,
                                    "rsi": r, "vr": vr, "pat": pat, "dist": dist})
    if r > (100 - RSI_LEVEL) and rel <= 15:
        ress = [s for s in find_levels(kl, "res") if s >= price * 0.998]
        if ress:
            lvl = min(ress, key=lambda s: abs(s - price))
            dist = abs(lvl - price) / lvl * 100
            if dist <= MAX_DIST and price <= lvl * 1.005:
                sl = lvl + a * ATR_BUF
                if sl > price:
                    risk = sl - price
                    rp = risk / price * 100
                    if 0 < rp <= MAX_RISK:
                        pat = detect_reversal(kl, "short") or "нет разворотной"
                        out.append({"side": "short", "sym": sym, "entry": price, "lvl": lvl,
                                    "sl": sl, "tp": price - risk * RR, "risk_pct": rp,
                                    "rsi": r, "vr": vr, "pat": pat, "dist": dist})
    return out

def fmt(p):
    if p >= 100: return format(p, ".2f")
    if p >= 1: return format(p, ".4f")
    return format(p, ".6f")

def pnl_usd(s, price):
    qty = SIZE / s["entry"]
    if s["side"] == "long":
        return qty * (price - s["entry"])
    return qty * (s["entry"] - price)

def card(s):
    qty = SIZE / s["entry"]
    risk_usd = qty * abs(s["entry"] - s["sl"])
    prof_usd = risk_usd * RR
    fees = SIZE * FEE / 100 * 2
    side_t = "🟢 ЛОНГ от поддержки" if s["side"] == "long" else "🔴 ШОРТ от сопротивления"
    lvl_t = "Поддержка" if s["side"] == "long" else "Сопротивление"
    pat_ok = "✅" if s["pat"] != "нет разворотной" else "⚠️"
    rsi_ok = "✅" if (s["rsi"] <= 32 or s["rsi"] >= 68) else "⚠️"
    return (
        "🎯 " + s["sym"] + " — " + side_t + " (4ч)\n\n"
        "✅ Свеча 4ч ЗАКРЫТА\n"
        + pat_ok + " Свеча: " + s["pat"] + "\n"
        + rsi_ok + " RSI: " + str(round(s["rsi"])) + "\n"
        "✅ Объём: ×" + format(s["vr"], ".1f") + "\n"
        "✅ У уровня: " + format(s["dist"], ".2f") + "%\n\n"
        "📍 Вход: " + fmt(s["entry"]) + "\n"
        "📊 " + lvl_t + ": " + fmt(s["lvl"]) + "\n"
        "🛑 Стоп: " + fmt(s["sl"]) + "  (риск " + format(s["risk_pct"], ".2f") + "%)\n"
        "🎯 Тейк: " + fmt(s["tp"]) + "  (1:3)\n\n"
        "💵 На $" + str(SIZE) + ":\n"
        "   риск ≈ -$" + str(round(risk_usd + fees)) + "\n"
        "   профит ≈ +$" + str(round(prof_usd - fees)) + "\n\n"
        "🛡 На +50% пути пришлю сигнал двинуть стоп в безубыток\n"
        "⚙️ плечо 1х!"
    )

def close_trade(chat_id, s, price, result):
    p = pnl_usd(s, price) - SIZE * FEE / 100 * 2
    history.setdefault(chat_id, [])
    history[chat_id].append({"sym": s["sym"], "side": s["side"], "result": result,
                             "pnl": round(p, 2), "entry": s["entry"], "exit": price,
                             "t": int(time.time())})
    return p

@bot.message_handler(commands=['start'])
def start(m):
    users.add(m.chat.id)
    save()
    bot.send_message(m.chat.id, "Отскок от уровней 4ч 🎯\nСигналы только по ЗАКРЫТЫМ свечам.\nATR-стоп, тейк 1:3, безубыток на полпути.\nВедётся статистика сделок.", reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🔍 Найти отскоки")
def btn_scan(m):
    users.add(m.chat.id)
    save()
    bot.send_message(m.chat.id, "Сканирую топ-200 монет на 4ч... жди 5-8 мин")
    do_scan(m.chat.id, manual=True)

@bot.message_handler(func=lambda m: m.text == "📋 Мои сделки")
def btn_list(m):
    t = tracked.get(m.chat.id, [])
    if not t:
        bot.send_message(m.chat.id, "Нет отслеживаемых сделок.", reply_markup=menu())
        return
    for s in list(t):
        try:
            p = get_price(s["sym"] + "USDT")
            pnl = pnl_usd(s, p)
            qty = SIZE / s["entry"]
            risk_usd = qty * abs(s["entry"] - s["sl"])
            prof_usd = qty * abs(s["tp"] - s["entry"])
            if s["side"] == "long":
                to_tp = (s["tp"] - p) / p * 100
                to_sl = (p - s["sl"]) / p * 100
            else:
                to_tp = (p - s["tp"]) / p * 100
                to_sl = (s["sl"] - p) / p * 100
            sign = "+" if pnl >= 0 else "-"
            emo = "🟢" if pnl >= 0 else "🔴"
            be = "\n🛡 стоп в безубытке" if s.get("be") else ""
            txt = (
                emo + " " + s["sym"] + " " + ("ЛОНГ" if s["side"] == "long" else "ШОРТ") + "\n\n"
                "💵 Сейчас: " + sign + "$" + format(abs(pnl), ".2f") + "\n"
                "📍 Вход: " + fmt(s["entry"]) + "\n"
                "💰 Цена: " + fmt(p) + "\n\n"
                "🛑 Стоп: " + fmt(s["sl"]) + "  (-$" + str(round(risk_usd)) + ")\n"
                "🎯 Тейк: " + fmt(s["tp"]) + "  (+$" + str(round(prof_usd)) + ")\n\n"
                "до тейка " + format(to_tp, ".2f") + "% · до стопа " + format(to_sl, ".2f") + "%"
                + be
            )
            bot.send_message(m.chat.id, txt, reply_markup=menu())
        except Exception:
            bot.send_message(m.chat.id, s["sym"] + ": не удалось получить цену")
        time.sleep(0.4)

@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def btn_stats(m):
    h = history.get(m.chat.id, [])
    if not h:
        bot.send_message(m.chat.id, "📊 Пока нет закрытых сделок.\nСтатистика появится после первого тейка или стопа.", reply_markup=menu())
        return
    total = len(h)
    wins = [x for x in h if x["result"] == "tp"]
    losses = [x for x in h if x["result"] == "sl"]
    bes = [x for x in h if x["result"] == "be"]
    money = sum(x["pnl"] for x in h)
    wr = round(len(wins) / total * 100)
    avg_w = round(np.mean([x["pnl"] for x in wins]), 1) if wins else 0
    avg_l = round(np.mean([abs(x["pnl"]) for x in losses]), 1) if losses else 0
    verdict = "✅ стратегия в плюсе" if money > 0 else "❌ пока в минусе"
    if total < 15:
        verdict += "\n⚠️ выборка мала (" + str(total) + "/20), рано судить"
    txt = (
        "📊 СТАТИСТИКА (" + str(total) + " сделок)\n\n"
        "🎯 Тейк: " + str(len(wins)) + " (" + str(wr) + "%)\n"
        "🛑 Стоп: " + str(len(losses)) + "\n"
        "🛡 Безубыток: " + str(len(bes)) + "\n\n"
        "💵 Итого: " + ("+" if money >= 0 else "") + "$" + str(round(money, 2)) + "\n"
        "Средний плюс: +$" + str(avg_w) + "\n"
        "Средний минус: -$" + str(avg_l) + "\n\n"
        + verdict + "\n\n"
        "Последние 5:\n"
    )
    for x in h[-5:][::-1]:
        ic = "🎯" if x["result"] == "tp" else ("🛑" if x["result"] == "sl" else "🛡")
        sg = "+" if x["pnl"] >= 0 else ""
        txt += ic + " " + x["sym"] + " " + sg + "$" + str(x["pnl"]) + "\n"
    bot.send_message(m.chat.id, txt, reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🗑 Очистить")
def btn_clear(m):
    tracked[m.chat.id] = []
    save()
    bot.send_message(m.chat.id, "Отслеживание снято. Статистика сохранена.", reply_markup=menu())

@bot.callback_query_handler(func=lambda c: c.data.startswith("t:"))
def cb_track(c):
    try:
        p = c.data.split(":")
        sym, side, entry, sl, tp = p[1], p[2], float(p[3]), float(p[4]), float(p[5])
        tracked.setdefault(c.message.chat.id, [])
        if any(x["sym"] == sym for x in tracked[c.message.chat.id]):
            bot.answer_callback_query(c.id, sym + " уже отслеживается")
            return
        tracked[c.message.chat.id].append({"sym": sym, "side": side, "entry": entry,
                                           "sl": sl, "tp": tp, "be": False})
        save()
        qty = SIZE / entry
        risk_usd = qty * abs(entry - sl)
        prof_usd = qty * abs(tp - entry)
        bot.answer_callback_query(c.id, "Отслеживаю " + sym)
        bot.send_message(c.message.chat.id,
            "✅ " + sym + " на отслеживании\n\n"
            "📍 Вход: " + fmt(entry) + "\n"
            "🛑 Стоп: " + fmt(sl) + "  (-$" + str(round(risk_usd)) + ")\n"
            "🎯 Тейк: " + fmt(tp) + "  (+$" + str(round(prof_usd)) + ")\n\n"
            "Пришлю алерт на тейк, стоп и безубыток.", reply_markup=menu())
    except Exception as e:
        print("track error:", e)
        bot.answer_callback_query(c.id, "Ошибка")

def do_scan(chat_id, manual=False):
    try:
        coins = top_coins()
        found = []
        for c in coins:
            try:
                res = analyze(c)
                if res:
                    found.extend(res)
            except Exception:
                pass
            time.sleep(0.12)
        sent_signals.setdefault(chat_id, {})
        fresh = []
        now = time.time()
        for s in found:
            key = s["sym"] + s["side"]
            if manual or (now - sent_signals[chat_id].get(key, 0) > 8 * 3600):
                fresh.append(s)
                sent_signals[chat_id][key] = now
        if not fresh:
            if manual:
                bot.send_message(chat_id, "Отскоков сейчас нет: цена ни у одного уровня (≤0.8%) при RSI на краю.", reply_markup=menu())
            return
        fresh.sort(key=lambda s: (s["pat"] == "нет разворотной", s["risk_pct"]))
        bot.send_message(chat_id, "Нашёл " + str(len(fresh)) + " отскок(ов):")
        for s in fresh[:8]:
            ikb = types.InlineKeyboardMarkup()
            cb = "t:" + s["sym"] + ":" + s["side"] + ":" + format(s["entry"], ".6f") + ":" + format(s["sl"], ".6f") + ":" + format(s["tp"], ".6f")
            if len(cb) <= 64:
                ikb.add(types.InlineKeyboardButton("➡️ Отслеживать " + s["sym"], callback_data=cb))
                bot.send_message(chat_id, card(s), reply_markup=ikb)
            else:
                bot.send_message(chat_id, card(s))
            time.sleep(1)
    except Exception as e:
        print("scan error:", e)
        if manual:
            bot.send_message(chat_id, "Ошибка при поиске, попробуй ещё раз.", reply_markup=menu())

def auto_loop():
    last = 0
    while True:
        try:
            changed = False
            for chat_id, lst in list(tracked.items()):
                for s in list(lst):
                    try:
                        p = get_price(s["sym"] + "USDT")
                        hit_tp = (p >= s["tp"]) if s["side"] == "long" else (p <= s["tp"])
                        hit_sl = (p <= s["sl"]) if s["side"] == "long" else (p >= s["sl"])
                        if hit_tp:
                            pl = close_trade(chat_id, s, s["tp"], "tp")
                            bot.send_message(chat_id, "🎯 ТЕЙК: " + s["sym"] + " дошёл до " + fmt(s["tp"]) + "!\nПрофит ≈ +$" + str(round(pl)) + "\nЗАКРЫВАЙ В ПЛЮС!", reply_markup=menu())
                            lst.remove(s); changed = True
                            continue
                        if hit_sl:
                            res = "be" if s.get("be") else "sl"
                            pl = close_trade(chat_id, s, s["sl"], res)
                            if res == "be":
                                msg = "🛡 БЕЗУБЫТОК: " + s["sym"] + " вернулся ко входу. Вышел ~в ноль."
                            else:
                                msg = "🛑 СТОП: " + s["sym"] + " пробил " + fmt(s["sl"]) + ".\nУбыток ≈ -$" + str(round(abs(pl))) + "\nЗАКРЫВАЙ!"
                            bot.send_message(chat_id, msg, reply_markup=menu())
                            lst.remove(s); changed = True
                            continue
                        if not s.get("be"):
                            half = s["entry"] + (s["tp"] - s["entry"]) * 0.5
                            reached = (p >= half) if s["side"] == "long" else (p <= half)
                            if reached:
                                s["sl"] = s["entry"]
                                s["be"] = True
                                changed = True
                                bot.send_message(chat_id,
                                    "🛡 БЕЗУБЫТОК: " + s["sym"] + " прошёл половину пути!\n"
                                    "Двигай стоп на вход: " + fmt(s["entry"]) + "\n"
                                    "Дальше сделка бесплатная.", reply_markup=menu())
                    except Exception:
                        pass
                    time.sleep(0.3)
            if changed:
                save()
            if time.time() - last > 1800:
                last = time.time()
                for uid in list(users):
                    try:
                        do_scan(uid, manual=False)
                    except Exception:
                        pass
        except Exception as e:
            print("loop error:", e)
        time.sleep(90)

load()
threading.Thread(target=auto_loop, daemon=True).start()
print("Бот отскоков запущен...")
bot.infinity_polling()
