import os
import json
import time
import hmac
import hashlib
import urllib.parse
import math
from datetime import datetime, timedelta, timezone
import threading
import telebot
from telebot import types
import requests
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

# ---------- Binance приватный доступ (демо/тестнет) ----------
BINANCE_KEY = os.environ.get("BINANCE_KEY", "").strip()
BINANCE_SECRET = os.environ.get("BINANCE_SECRET", "").strip()
FAPI = os.environ.get("FAPI_BASE", "https://testnet.binancefuture.com").rstrip("/")

# с какого момента считаем статистику. Время МОСКОВСКОЕ.
# форматы: "2026-07-17 20:00" или "2026-07-17"
STATS_START = os.environ.get("STATS_START", "2026-07-17 20:00").strip()
MSK = timezone(timedelta(hours=3))

SIZE = 1000
RSI_LEVEL = 40
VOL_MULT = 1.1
MAX_DIST = 0.8
MAX_RISK = 3.0
RR = 3
ATR_BUF = 0.5
MAX_COINS = 200
FEE = 0.055
BE_BAND = 5.0        # ±$5 вокруг нуля считаем безубытком
BTC_FLAT = 0.7       # ±0.7% от EMA50 — рынок без выраженного направления

# ---------- АВТОТОРГОВЛЯ ----------
AUTO_TRADE = os.environ.get("AUTO_TRADE", "0") == "1"   # рубильник в Railway
AUTO_MIN_PTS = int(os.environ.get("AUTO_MIN_PTS", "5"))  # 5 = только 🟢, 3 = 🟢+🟡
MAX_POS = int(os.environ.get("MAX_POS", "4"))            # потолок одновременных позиций
LEVERAGE = int(os.environ.get("LEVERAGE", "5"))          # маржа; нотионал всегда SIZE
auto_on = AUTO_TRADE
opened_keys = set()   # защита от дублей при перезапуске

# если подключишь Railway Volume и укажешь DATA_DIR=/data — статистика переживёт редеплой
DATA_DIR = os.environ.get("DATA_DIR", ".").rstrip("/")
DB = DATA_DIR + "/trades.json"

tracked = {}
users = set()
sent_signals = {}
history = {}   # {chat_id: [ {sym, side, result, pnl, entry, exit, fee, fund, src} ]}


def has_keys():
    return bool(BINANCE_KEY and BINANCE_SECRET)


_offset = {"ms": 0}


def sync_time():
    try:
        r = requests.get(FAPI + "/fapi/v1/time", timeout=10).json()
        _offset["ms"] = int(r["serverTime"]) - int(time.time() * 1000)
        print("время синхронизировано, сдвиг", _offset["ms"], "мс")
    except Exception as e:
        print("time sync error:", e)


def signed_post(path, params=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000) + _offset["ms"]
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(p)
    sig = hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    r = requests.post(FAPI + path + "?" + q + "&signature=" + sig,
                      headers={"X-MBX-APIKEY": BINANCE_KEY}, timeout=20)
    d = r.json()
    if isinstance(d, dict) and "code" in d and "msg" in d and d.get("code") != 200:
        raise Exception("Binance: " + str(d.get("code")) + " " + str(d.get("msg")))
    return d


_filters = {}


def load_filters():
    """шаг лота, шаг цены и мин. нотионал по каждому фьючерсному символу"""
    try:
        d = requests.get(FAPI + "/fapi/v1/exchangeInfo", timeout=25).json()
        for s in d.get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
                continue
            if s.get("status") != "TRADING":
                continue
            f = {"step": 0.001, "tick": 0.0001, "minQty": 0.0, "minNot": 5.0}
            for x in s.get("filters", []):
                t = x.get("filterType")
                if t == "LOT_SIZE":
                    f["step"] = float(x["stepSize"]); f["minQty"] = float(x["minQty"])
                elif t == "PRICE_FILTER":
                    f["tick"] = float(x["tickSize"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    f["minNot"] = float(x.get("notional") or x.get("minNotional") or 5)
            _filters[s["symbol"]] = f
        print("фильтров загружено:", len(_filters))
    except Exception as e:
        print("exchangeInfo error:", e)


def step_round(v, step):
    if step <= 0:
        return v
    n = math.floor(round(v / step, 8)) * step
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n, d)


def auto_symbol(sym):
    """торгуем только прямые SYM+USDT — у 1000-контрактов другой масштаб цен"""
    s = sym + "USDT"
    return s if s in _filters else None


def open_trade(chat_id, s):
    """рыночный вход + TP и SL ордерами на бирже"""
    fs = auto_symbol(s["sym"])
    if not fs:
        return False, "нет прямого фьючерса"
    key = fs + s["side"] + str(int(s["entry"] * 1e6))
    if key in opened_keys:
        return False, "уже открывали"
    f = _filters[fs]

    try:
        signed_post("/fapi/v1/leverage", {"symbol": fs, "leverage": LEVERAGE})
    except Exception as e:
        print("leverage:", e)

    price = get_price(fs)
    qty = step_round(SIZE / price, f["step"])
    if qty < f["minQty"] or qty * price < f["minNot"]:
        return False, "размер меньше минимума"

    side = "BUY" if s["side"] == "long" else "SELL"
    opp = "SELL" if side == "BUY" else "BUY"
    cid = "otsk" + str(abs(hash(key)) % 10**10)

    signed_post("/fapi/v1/order", {"symbol": fs, "side": side, "type": "MARKET",
                                   "quantity": qty, "newClientOrderId": cid})
    opened_keys.add(key)

    tp = step_round(s["tp"], f["tick"])
    sl = step_round(s["sl"], f["tick"])
    warn = ""
    for typ, sp in (("TAKE_PROFIT_MARKET", tp), ("STOP_MARKET", sl)):
        try:
            signed_post("/fapi/v1/order", {"symbol": fs, "side": opp, "type": typ,
                                           "stopPrice": sp, "closePosition": "true",
                                           "workingType": "MARK_PRICE",
                                           "timeInForce": "GTE_GTC"})
        except Exception as e:
            warn += "\n⚠️ не встал " + typ + ": " + str(e)
    return True, ("qty " + str(qty) + " · $" + str(round(qty * price)) + warn)


def signed_get(path, params=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000) + _offset["ms"]
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(p)
    sig = hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = FAPI + path + "?" + q + "&signature=" + sig
    r = requests.get(url, headers={"X-MBX-APIKEY": BINANCE_KEY}, timeout=20)
    d = r.json()
    if isinstance(d, dict) and "code" in d and "msg" in d:
        raise Exception("Binance: " + str(d.get("code")) + " " + str(d.get("msg")))
    return d


def binance_positions():
    """{'SYMBOL': {'amt':..,'entry':..,'upnl':..}} только реально открытые"""
    d = signed_get("/fapi/v2/positionRisk")
    out = {}
    for x in d:
        try:
            amt = float(x.get("positionAmt", 0))
        except Exception:
            continue
        if abs(amt) > 0:
            out[x["symbol"]] = {"amt": amt,
                                "entry": float(x.get("entryPrice", 0) or 0),
                                "upnl": float(x.get("unRealizedProfit", 0) or 0)}
    return out


def binance_income(start_ms, symbol=None, end_ms=None):
    p = {"startTime": int(start_ms), "limit": 1000}
    if end_ms:
        p["endTime"] = int(end_ms)
    if symbol:
        p["symbol"] = symbol
    d = signed_get("/fapi/v1/income", p)
    return d if isinstance(d, list) else []


def stats_start_ms():
    """STATS_START задаётся по Москве, Binance хочет миллисекунды UTC"""
    s = STATS_START.replace("T", " ").strip()
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, f).replace(tzinfo=MSK)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    print("STATS_START не разобран, беру вчера:", STATS_START)
    now = int(time.time())
    return ((now - (now % 86400)) - 86400) * 1000


def income_all(start_ms, symbol=None):
    """Binance отдаёт максимум 7 дней за запрос — тянем кусками"""
    out = []
    week = 7 * 86400 * 1000
    cur = int(start_ms)
    end = int(time.time() * 1000) + 60000
    guard = 0
    while cur < end and guard < 30:
        guard += 1
        stop = min(cur + week, end)
        try:
            out.extend(binance_income(cur, symbol, stop))
        except Exception as e:
            print("income chunk error:", e)
        cur = stop
        time.sleep(0.2)
    return out


def split_income(rows):
    r = {"pnl": 0.0, "fee": 0.0, "fund": 0.0, "other": 0.0, "n": 0}
    for x in rows:
        try:
            v = float(x.get("income", 0))
        except Exception:
            continue
        t = x.get("incomeType", "")
        if t == "REALIZED_PNL":
            r["pnl"] += v
            r["n"] += 1
        elif t == "COMMISSION":
            r["fee"] += v
        elif t == "FUNDING_FEE":
            r["fund"] += v
        else:
            r["other"] += v
    return r


def fut_candidates(sym):
    """на фьючах бывают 1000-контракты: SHIB -> 1000SHIBUSDT"""
    return [sym + "USDT", "1000" + sym + "USDT"]


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
        print("загружено: сделок", sum(len(v) for v in tracked.values()),
              "| история", sum(len(v) for v in history.values()))
    except Exception:
        print("нет сохранённых данных, старт с нуля")


def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти отскоки")
    kb.row("📋 Мои сделки", "📊 Статистика")
    kb.row("🤖 Автоторговля")
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


def btc_regime():
    """куда смотрит рынок: считаем по BTC 4ч против EMA50"""
    try:
        raw = get_klines("BTCUSDT", 120)
        now_ms = int(time.time() * 1000)
        kl = raw[:-1] if raw[-1]["close_t"] > now_ms else raw
        closes = [k["c"] for k in kl]
        e50 = ema(closes, 50)
        if not e50:
            return "flat", 0.0
        p = closes[-1]
        d = (p - e50) / e50 * 100
        if d < -BTC_FLAT:
            return "bear", d
        if d > BTC_FLAT:
            return "bull", d
        return "flat", d
    except Exception as e:
        print("btc regime error:", e)
        return "flat", 0.0


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


def money(v):
    return ("+" if v >= 0 else "-") + "$" + format(abs(v), ".2f")


def pnl_usd(s, price):
    qty = SIZE / s["entry"]
    if s["side"] == "long":
        return qty * (price - s["entry"])
    return qty * (s["entry"] - price)


def grade(s):
    """оценка входа: считаем, сколько условий отработали идеально"""
    pts = 0
    if s["pat"] != "нет разворотной":
        pts += 2                                  # разворотная свеча — главное
    if s["rsi"] <= 32 or s["rsi"] >= 68:
        pts += 1                                  # RSI на самом краю
    if s["vr"] >= 1.5:
        pts += 1                                  # объём заметно выше нормы
    if s["dist"] <= 0.4:
        pts += 1                                  # вплотную к уровню
    if s["risk_pct"] <= 1.5:
        pts += 1                                  # короткий стоп
    if pts >= 5:
        return pts, "🟢 ХОРОШАЯ ПАРА"
    if pts >= 3:
        return pts, "🟡 СРЕДНЯЯ ПАРА"
    return pts, "🔴 СЛАБАЯ ПАРА"


def card(s):
    qty = SIZE / s["entry"]
    risk_usd = qty * abs(s["entry"] - s["sl"])
    prof_usd = risk_usd * RR
    fees = SIZE * FEE / 100 * 2
    side_t = "🟢 ЛОНГ от поддержки" if s["side"] == "long" else "🔴 ШОРТ от сопротивления"
    lvl_t = "Поддержка" if s["side"] == "long" else "Сопротивление"
    pat_ok = "✅" if s["pat"] != "нет разворотной" else "⚠️"
    rsi_ok = "✅" if (s["rsi"] <= 32 or s["rsi"] >= 68) else "⚠️"
    vol_ok = "✅" if s["vr"] >= 1.5 else "⚠️"
    dst_ok = "✅" if s["dist"] <= 0.4 else "⚠️"
    rsk_ok = "✅" if s["risk_pct"] <= 1.5 else "⚠️"
    pts, lab = grade(s)
    return (
        "🎯 " + s["sym"] + " — " + side_t + " (4ч)\n"
        + lab + "  (" + str(pts) + "/6)\n\n"
        + pat_ok + " Свеча: " + s["pat"] + "\n"
        + rsi_ok + " RSI: " + str(round(s["rsi"])) + "\n"
        + vol_ok + " Объём: ×" + format(s["vr"], ".1f") + "\n"
        + dst_ok + " У уровня: " + format(s["dist"], ".2f") + "%\n"
        + rsk_ok + " Стоп: " + format(s["risk_pct"], ".2f") + "%\n\n"
        "📍 Вход: " + fmt(s["entry"]) + "\n"
        "📊 " + lvl_t + ": " + fmt(s["lvl"]) + "\n"
        "🛑 Стоп: " + fmt(s["sl"]) + "\n"
        "🎯 Тейк: " + fmt(s["tp"]) + "  (1:3)\n\n"
        "💵 На $" + str(SIZE) + ":\n"
        "   риск ≈ -$" + str(round(risk_usd + fees)) + "\n"
        "   профит ≈ +$" + str(round(prof_usd - fees)) + "\n\n"
        "🛡 На +50% пути пришлю сигнал двинуть стоп в безубыток\n"
        "⚙️ плечо 1х!"
    )


def close_trade(chat_id, s, price, result):
    """расчётное закрытие (когда позиции на бирже не видно)"""
    p = pnl_usd(s, price) - SIZE * FEE / 100 * 2
    history.setdefault(chat_id, [])
    history[chat_id].append({"sym": s["sym"], "side": s["side"], "result": result,
                             "pnl": round(p, 2), "entry": s["entry"], "exit": price,
                             "fee": round(-SIZE * FEE / 100 * 2, 2), "fund": 0.0,
                             "src": "calc", "t": int(time.time())})
    return p


def close_trade_real(chat_id, s, inc, result):
    """реальное закрытие по данным Binance"""
    net = inc["pnl"] + inc["fee"] + inc["fund"]
    history.setdefault(chat_id, [])
    history[chat_id].append({"sym": s["sym"], "side": s["side"], "result": result,
                             "pnl": round(net, 2), "entry": s.get("bn_entry", s["entry"]),
                             "exit": 0, "gross": round(inc["pnl"], 2),
                             "fee": round(inc["fee"], 2), "fund": round(inc["fund"], 2),
                             "src": "binance", "t": int(time.time())})
    return net


def classify(s, net):
    """тейк / стоп / безубыток по реальному результату"""
    qty = s.get("bn_qty") or (SIZE / s["entry"])
    ent = s.get("bn_entry") or s["entry"]
    risk = qty * abs(ent - s["sl"])
    if risk > 0 and abs(net) < risk * 0.25:
        return "be"
    return "tp" if net > 0 else "sl"


# ---------- хендлеры ----------

@bot.message_handler(commands=['start'])
def start(m):
    users.add(m.chat.id)
    save()
    k = ("✅ Binance подключён — статистика с " + STATS_START + " МСК по реальным сделкам.") if has_keys() \
        else "⚠️ Binance не подключён — добавь ключи в Railway."
    bot.send_message(m.chat.id,
        "Отскок от уровней 4ч 🎯\nСигналы только по ЗАКРЫТЫМ свечам.\n"
        "ATR-стоп, тейк 1:3, безубыток на полпути.\n\n" + k, reply_markup=menu())


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
            emo = "🟢" if pnl >= 0 else "🔴"
            be = "\n🛡 стоп в безубытке" if s.get("be") else ""
            if s.get("bn"):
                link = "\n🔗 открыта на Binance (" + s.get("bn_sym", "") + ")"
            elif has_keys():
                link = "\n⏳ на Binance пока не вижу позицию"
            else:
                link = ""
            txt = (
                emo + " " + s["sym"] + " " + ("ЛОНГ" if s["side"] == "long" else "ШОРТ") + "\n\n"
                "💵 Расчётно: " + money(pnl) + "\n"
                "📍 Вход: " + fmt(s["entry"]) + "\n"
                "💰 Цена: " + fmt(p) + "\n\n"
                "🛑 Стоп: " + fmt(s["sl"]) + "  (-$" + str(round(risk_usd)) + ")\n"
                "🎯 Тейк: " + fmt(s["tp"]) + "  (+$" + str(round(prof_usd)) + ")\n\n"
                "до тейка " + format(to_tp, ".2f") + "% · до стопа " + format(to_sl, ".2f") + "%"
                + be + link
            )
            bot.send_message(m.chat.id, txt, reply_markup=menu())
        except Exception:
            bot.send_message(m.chat.id, s["sym"] + ": не удалось получить цену")
        time.sleep(0.4)


@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def btn_stats(m):
    if not has_keys():
        bot.send_message(m.chat.id, "⚠️ Binance не подключён.\nДобавь BINANCE_KEY и BINANCE_SECRET в Railway → Variables.", reply_markup=menu())
        return
    bot.send_message(m.chat.id, "Тяну реальные сделки с Binance...")
    try:
        rows = income_all(stats_start_ms())
        if not rows:
            bot.send_message(m.chat.id, "📊 С " + STATS_START + " МСК закрытых сделок нет.", reply_markup=menu())
            return

        tot = split_income(rows)
        real = tot["pnl"] + tot["fee"] + tot["fund"] + tot["other"]

        by = {}
        for x in rows:
            by.setdefault(x.get("symbol") or "—", []).append(x)
        trades = []
        for sym, rr in by.items():
            d = split_income(rr)
            if d["n"] == 0:
                continue
            trades.append(d["pnl"] + d["fee"] + d["fund"])

        total = len(trades)
        wins = [x for x in trades if x > BE_BAND]
        losses = [x for x in trades if x < -BE_BAND]
        bes = [x for x in trades if abs(x) <= BE_BAND]
        wr = (len(wins) / total * 100) if total else 0
        avg_w = float(np.mean(wins)) if wins else 0.0
        avg_l = float(np.mean([abs(x) for x in losses])) if losses else 0.0

        try:
            pos = binance_positions()
        except Exception:
            pos = {}
        upnl = sum(v["upnl"] for v in pos.values())

        L = []
        L.append("📊 ОТСКОК 1:3 · с " + STATS_START + " МСК")
        L.append(str(total) + " монет · " + str(tot["n"]) + " закрытий")
        L.append("")
        L.append("💵 Чистыми:  " + money(real))
        if pos:
            L.append("📌 Плавает:  " + money(upnl) + "  (" + str(len(pos)) + ")")
            L.append("━━━━━━━━━━━━")
            L.append("ИТОГО:       " + money(real + upnl))
        L.append("")
        L.append("🟢 В плюс:   " + str(len(wins)) + "   (" + format(wr, ".0f") + "%)")
        L.append("🔴 В минус:  " + str(len(losses)))
        L.append("🛡 В ноль:   " + str(len(bes)))
        L.append("")
        L.append("Средний плюс:  " + money(avg_w))
        L.append("Средний минус: " + money(-avg_l))
        L.append("")
        L.append("📈 Грязный: " + money(tot["pnl"]))
        L.append("💸 Комиссии: " + money(tot["fee"]))
        L.append("💱 Фандинг: " + money(tot["fund"]))
        L.append("")
        days = len({int(x.get("time", 0)) // 86400000 for x in rows
                    if x.get("incomeType") == "REALIZED_PNL"})
        cpc = (tot["n"] / total) if total else 0
        L.append("Выборка: " + str(total) + " сделок за " + str(days) + " дн.")
        L.append("Закрытий на монету: " + format(cpc, ".1f"))

        bot.send_message(m.chat.id, "\n".join(L), reply_markup=menu())
    except Exception as e:
        bot.send_message(m.chat.id, "Ошибка Binance: " + str(e) +
                         "\n\nПроверь ключи от testnet.binancefuture.com.", reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🤖 Автоторговля")
def btn_auto(m):
    global auto_on
    if not has_keys():
        bot.send_message(m.chat.id, "⚠️ Нет ключей Binance.", reply_markup=menu())
        return
    auto_on = not auto_on
    if auto_on:
        try:
            pos = len(binance_positions())
        except Exception:
            pos = "?"
        grd = "только 🟢 (5+/6)" if AUTO_MIN_PTS >= 5 else "🟢 и 🟡 (" + str(AUTO_MIN_PTS) + "+/6)"
        bot.send_message(m.chat.id,
            "🤖 АВТОТОРГОВЛЯ ВКЛЮЧЕНА\n\n"
            "Вхожу: " + grd + "\n"
            "Размер: $" + str(SIZE) + " · плечо " + str(LEVERAGE) + "x\n"
            "Потолок: " + str(MAX_POS) + " позиций (сейчас " + str(pos) + ")\n"
            "TP/SL ставлю ордерами сразу при входе\n\n"
            "Фильтр BTC работает. Руками не трогай — иначе статистика опять поедет.",
            reply_markup=menu())
    else:
        bot.send_message(m.chat.id, "🤖 Автоторговля ВЫКЛЮЧЕНА.\nОткрытые позиции не трогаю — их закроют TP/SL.", reply_markup=menu())


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
                                           "sl": sl, "tp": tp, "be": False,
                                           "bn": False, "a_tp": False, "a_sl": False,
                                           "t0": int(time.time() * 1000)})
        save()
        qty = SIZE / entry
        risk_usd = qty * abs(entry - sl)
        prof_usd = qty * abs(tp - entry)
        bot.answer_callback_query(c.id, "Отслеживаю " + sym)
        tail = "\nКак откроешь на Binance — подхвачу позицию и буду считать по факту." if has_keys() else ""
        bot.send_message(c.message.chat.id,
            "✅ " + sym + " на отслеживании\n\n"
            "📍 Вход: " + fmt(entry) + "\n"
            "🛑 Стоп: " + fmt(sl) + "  (-$" + str(round(risk_usd)) + ")\n"
            "🎯 Тейк: " + fmt(tp) + "  (+$" + str(round(prof_usd)) + ")\n\n"
            "Пришлю алерт на тейк, стоп и безубыток." + tail, reply_markup=menu())
    except Exception as e:
        print("track error:", e)
        bot.answer_callback_query(c.id, "Ошибка")


def do_scan(chat_id, manual=False):
    try:
        mode, dev = btc_regime()
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

        # фильтр по битку: не ловим нож против рынка
        cut = 0
        if mode == "bear":
            n0 = len(found)
            found = [s for s in found if s["side"] == "short"]
            cut = n0 - len(found)
        elif mode == "bull":
            n0 = len(found)
            found = [s for s in found if s["side"] == "long"]
            cut = n0 - len(found)

        sent_signals.setdefault(chat_id, {})
        # анти-дубль: монета уже в трекинге -> сигнал не шлём
        busy = {x["sym"] for x in tracked.get(chat_id, [])}
        skipped = 0
        fresh = []
        now = time.time()
        for s in found:
            if s["sym"] in busy:
                skipped += 1
                continue
            key = s["sym"] + s["side"]
            if manual or (now - sent_signals[chat_id].get(key, 0) > 8 * 3600):
                fresh.append(s)
                sent_signals[chat_id][key] = now
        if mode == "bear":
            btc_t = "📉 BTC ниже EMA50 (" + format(dev, ".1f") + "%) — только шорты"
        elif mode == "bull":
            btc_t = "📈 BTC выше EMA50 (+" + format(dev, ".1f") + "%) — только лонги"
        else:
            btc_t = "➖ BTC у EMA50 (" + format(dev, ".1f") + "%) — обе стороны"
        if not fresh:
            if manual:
                extra = btc_t
                if cut:
                    extra += "\n(" + str(cut) + " скрыто против BTC)"
                if skipped:
                    extra += "\n(" + str(skipped) + " уже в трекинге)"
                bot.send_message(chat_id, "Отскоков сейчас нет.\n" + extra, reply_markup=menu())
            return
        fresh.sort(key=lambda s: (-grade(s)[0], s["risk_pct"]))
        g_cnt = {}
        for s in fresh:
            g_cnt[grade(s)[1][0]] = g_cnt.get(grade(s)[1][0], 0) + 1
        head = ("Нашёл " + str(len(fresh)) + ": 🟢" + str(g_cnt.get("🟢", 0))
                + " 🟡" + str(g_cnt.get("🟡", 0)) + " 🔴" + str(g_cnt.get("🔴", 0))
                + "\n" + btc_t)
        if cut:
            head += "\n(" + str(cut) + " скрыто против BTC)"
        if skipped:
            head += "\n(" + str(skipped) + " уже в трекинге)"
        bot.send_message(chat_id, head)
        if auto_on and has_keys():
            auto_enter(chat_id, fresh)

        for s in fresh[:8]:
            ikb = types.InlineKeyboardMarkup()
            cb = "t:" + s["sym"] + ":" + s["side"] + ":" + format(s["entry"], ".6f") + ":" + format(s["sl"], ".6f") + ":" + format(s["tp"], ".6f")
            if len(cb) <= 64:
                ikb.add(types.InlineKeyboardButton(grade(s)[1][0] + " Отслеживать " + s["sym"], callback_data=cb))
                bot.send_message(chat_id, card(s), reply_markup=ikb)
            else:
                bot.send_message(chat_id, card(s))
            time.sleep(1)
    except Exception as e:
        print("scan error:", e)
        if manual:
            bot.send_message(chat_id, "Ошибка при поиске, попробуй ещё раз.", reply_markup=menu())


def auto_enter(chat_id, fresh):
    """открываем сами: только сильные сигналы, не больше MAX_POS позиций"""
    try:
        pos = binance_positions()
    except Exception as e:
        print("auto positions:", e)
        return
    free = MAX_POS - len(pos)
    if free <= 0:
        bot.send_message(chat_id, "🤖 занято " + str(len(pos)) + "/" + str(MAX_POS) + " — не вхожу")
        return
    for s in fresh:
        if free <= 0:
            break
        if grade(s)[0] < AUTO_MIN_PTS:
            continue
        fs = auto_symbol(s["sym"])
        if not fs or fs in pos:
            continue
        try:
            ok, info = open_trade(chat_id, s)
        except Exception as e:
            bot.send_message(chat_id, "🤖 ❌ " + s["sym"] + ": " + str(e))
            continue
        if not ok:
            continue
        free -= 1
        tracked.setdefault(chat_id, [])
        if not any(x["sym"] == s["sym"] for x in tracked[chat_id]):
            tracked[chat_id].append({"sym": s["sym"], "side": s["side"], "entry": s["entry"],
                                     "sl": s["sl"], "tp": s["tp"], "be": False, "bn": False,
                                     "a_tp": False, "a_sl": False, "auto": True,
                                     "t0": int(time.time() * 1000)})
        save()
        bot.send_message(chat_id,
            "🤖 ОТКРЫЛ " + s["sym"] + " " + ("ЛОНГ" if s["side"] == "long" else "ШОРТ")
            + "  " + grade(s)[1] + "\n"
            "📍 " + fmt(s["entry"]) + " · " + info + "\n"
            "🛑 " + fmt(s["sl"]) + "  🎯 " + fmt(s["tp"]) + "\n"
            "TP и SL стоят ордерами. Руками не трогаем.", reply_markup=menu())
        time.sleep(1)


def sync_binance():
    """сверяем трекинг с реальными позициями на бирже"""
    if not has_keys() or not tracked:
        return False
    try:
        pos = binance_positions()
    except Exception as e:
        print("positions error:", e)
        return False
    changed = False
    for chat_id, lst in list(tracked.items()):
        for s in list(lst):
            hit = None
            for cand in fut_candidates(s["sym"]):
                if cand in pos:
                    hit = cand
                    break
            if hit:
                if not s.get("bn"):
                    s["bn"] = True
                    s["bn_sym"] = hit
                    s["bn_qty"] = abs(pos[hit]["amt"])
                    s["bn_entry"] = pos[hit]["entry"] or s["entry"]
                    s["bn_t"] = int(time.time() * 1000) - 10 * 60 * 1000
                    changed = True
                    try:
                        bot.send_message(chat_id, "🔗 " + s["sym"] + " вижу на Binance ("
                                         + hit + ", " + ("ЛОНГ" if pos[hit]["amt"] > 0 else "ШОРТ")
                                         + ", вход " + fmt(pos[hit]["entry"]) + ")\n"
                                         "Дальше считаю по реальным данным биржи.")
                    except Exception:
                        pass
            else:
                if s.get("bn"):
                    # позиция была, теперь её нет -> биржа закрыла
                    try:
                        rows = binance_income(s.get("bn_t", int(time.time() * 1000) - 86400000),
                                              s.get("bn_sym", s["sym"] + "USDT"))
                        inc = split_income(rows)
                    except Exception as e:
                        print("income error:", e)
                        inc = {"pnl": 0.0, "fee": 0.0, "fund": 0.0, "other": 0.0, "n": 0}
                    net = inc["pnl"] + inc["fee"] + inc["fund"]
                    res = classify(s, net)
                    close_trade_real(chat_id, s, inc, res)
                    lst.remove(s)
                    changed = True
                    ic = "🎯" if res == "tp" else ("🛑" if res == "sl" else "🛡")
                    try:
                        bot.send_message(chat_id,
                            ic + " " + s["sym"] + " ЗАКРЫТА НА BINANCE\n\n"
                            "📈 Грязный PnL: " + money(inc["pnl"]) + "\n"
                            "💸 Комиссии: " + money(inc["fee"]) + "\n"
                            "💱 Фандинг: " + money(inc["fund"]) + "\n"
                            "━━━━━━━━━━\n"
                            "💵 Чистыми: " + money(net) + "\n\n"
                            "Убрал из трекинга, записал в статистику.", reply_markup=menu())
                    except Exception:
                        pass
    return changed


def auto_loop():
    last = 0
    last_sync = 0
    while True:
        try:
            changed = False
            if time.time() - last_sync > 60:
                last_sync = time.time()
                if sync_binance():
                    changed = True

            for chat_id, lst in list(tracked.items()):
                for s in list(lst):
                    try:
                        p = get_price(s["sym"] + "USDT")
                        hit_tp = (p >= s["tp"]) if s["side"] == "long" else (p <= s["tp"])
                        hit_sl = (p <= s["sl"]) if s["side"] == "long" else (p >= s["sl"])
                        on_bn = s.get("bn")

                        if hit_tp:
                            if on_bn:
                                # позицию ведёт биржа: только напоминаем, из трекинга не убираем
                                if not s.get("a_tp"):
                                    s["a_tp"] = True
                                    changed = True
                                    bot.send_message(chat_id, "🎯 ТЕЙК: " + s["sym"] + " дошёл до " + fmt(s["tp"]) + "!\nЗАКРЫВАЙ В ПЛЮС.\nКак закроешь — посчитаю реальный итог.", reply_markup=menu())
                            else:
                                pl = close_trade(chat_id, s, s["tp"], "tp")
                                bot.send_message(chat_id, "🎯 ТЕЙК: " + s["sym"] + " дошёл до " + fmt(s["tp"]) + "!\nПрофит ≈ " + money(pl) + "\nЗАКРЫВАЙ В ПЛЮС!", reply_markup=menu())
                                lst.remove(s); changed = True
                            continue

                        if hit_sl:
                            res = "be" if s.get("be") else "sl"
                            if on_bn:
                                if not s.get("a_sl"):
                                    s["a_sl"] = True
                                    changed = True
                                    msg = ("🛡 БЕЗУБЫТОК: " + s["sym"] + " вернулся ко входу." ) if res == "be" \
                                        else ("🛑 СТОП: " + s["sym"] + " пробил " + fmt(s["sl"]) + ".\nЗАКРЫВАЙ!")
                                    bot.send_message(chat_id, msg + "\nКак закроешь — посчитаю реальный итог.", reply_markup=menu())
                            else:
                                pl = close_trade(chat_id, s, s["sl"], res)
                                if res == "be":
                                    msg = "🛡 БЕЗУБЫТОК: " + s["sym"] + " вернулся ко входу. Вышел ~в ноль."
                                else:
                                    msg = "🛑 СТОП: " + s["sym"] + " пробил " + fmt(s["sl"]) + ".\nУбыток ≈ " + money(pl) + "\nЗАКРЫВАЙ!"
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
if has_keys():
    sync_time()
    load_filters()
    try:
        p = binance_positions()
        print("Binance OK, открытых позиций:", len(p))
    except Exception as e:
        print("Binance ключи не работают:", e)
    print("автоторговля:", "ВКЛ" if auto_on else "выкл",
          "| мин. оценка", AUTO_MIN_PTS, "| потолок", MAX_POS, "| плечо", LEVERAGE)
else:
    print("Binance ключи не заданы — работаю в расчётном режиме")
threading.Thread(target=auto_loop, daemon=True).start()
print("Бот отскоков запущен...")
bot.infinity_polling()
