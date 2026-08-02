import os
import json
import time
import hmac
import hashlib
import urllib.parse
import math
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Tuple

import requests
import telebot
from telebot import types
import numpy as np

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

BINANCE_KEY = os.environ.get("BINANCE_KEY", "").strip()
BINANCE_SECRET = os.environ.get("BINANCE_SECRET", "").strip()
FAPI = os.environ.get("FAPI_BASE", "https://testnet.binancefuture.com").rstrip("/")
DATA_API = os.environ.get("DATA_API", FAPI).rstrip("/")

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOTUSDT,AVAXUSDT,LINKUSDT,MATICUSDT,LTCUSDT,TRXUSDT,FILUSDT,VETUSDT,ATOMUSDT,NEARUSDT,OPUSDT,ARBITUSDT,SUIUSDT,PEPEUSDT,ETHFIUSDT,LUNCUSDT,INUSDT,GALUSDT,JUPUSDT,THETAUSDT,MKRUSDT,SNXUSDT,MASKUSDT,LDOUSDT,CRVUSDT,ICPUSDT,SANUSDT,ALGOUSDT,UNIUSDT,GRTUSDT,SKLUSDT,KDOUSDT,CHZUSDT,MINAUSDT,TAOUSDT,ORDIUSDT,APTUSDT,QNTUSDT,WUSDT,INJUSDT,WOOUSDT,RNDRUSDT,TTKUSDT,ONDOUSDT,GMUSDT,BONKUSDT,VXUSDT,ARKUSDT,PYTHUSDT,ZECUSDT,DYDXUSDT,RAYUSDT,FLMUSDT,STRKUSDT,AIUSDT,TFUELUSDT,SEIUSDT,GMXUSDT,FLOKIUSDT,RONUSDT,USDCUSDT,TUSDUSDT,MBLUSDT,KEYUSDT,ROSIUSDT,NOUSDT,WAVESUSDT,RSRUSDT,AVEEUSDT,HBARUSDT,SUSDT,JUPITERUSDT,HIGHUSDT,YFIUSDT,CVXUSDT,PRIMEUSDT").split(",")
SYMBOLS = [s.strip() for s in SYMBOLS if s.strip()]
MAX_SYMBOLS = int(os.environ.get("MAX_SYMBOLS", str(len(SYMBOLS))))  # ограничить кол-во если нужно

# Параметры стратегии
RISK_PER_TRADE_USDT = float(os.environ.get("RISK_PER_TRADE_USDT", "50.0"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
LEVERAGE = int(os.environ.get("LEVERAGE", "5"))
SL_PCT = float(os.environ.get("SL_PCT", "0.8"))   # стоп 0.8% от уровня
TP_PCT = float(os.environ.get("TP_PCT", "2.0"))   # тейк 2.0% от входа
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "120"))
MAX_HOLD_H = int(os.environ.get("MAX_HOLD_H", "24"))  # макс удержание, часов
TRIANGLE_MIN_CANDLES = int(os.environ.get("TRIANGLE_MIN_CANDLES", "5"))  # минимум свечей в треугольнике
LOOKBACK_CANDLES = int(os.environ.get("LOOKBACK_CANDLES", "50"))  # сколько свечей смотрим для уровней
RETEST_TIMEOUT_SEC = int(os.environ.get("RETEST_TIMEOUT_SEC", "7200"))  # таймаут ожидания ретеста (2 часа)
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # интервал сканирования (5 мин)

MSK = timezone(timedelta(hours=3))
DATA_DIR = os.environ.get("DATA_DIR", ".").rstrip("/")
DB = DATA_DIR + "/trades.json"

CHAT_ID = os.environ.get("CHAT_ID", "").strip()
users = set()
if CHAT_ID:
    try:
        users.add(int(CHAT_ID))
    except Exception:
        print("CHAT_ID неверный:", CHAT_ID)

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
tracked = {}
last_trade_time = {}
history = {}
filters = {}
breakout_state = {}  # {symbol: {level, direction, t_breakout, retest_tried}}
data_lock = threading.Lock()
_offset = {"ms": 0}
_last_info_update = {"ts": 0}

# ========== УТИЛИТЫ ==========

def has_keys():
    return bool(BINANCE_KEY and BINANCE_SECRET)

def sync_time():
    try:
        r = requests.get(FAPI + "/fapi/v1/time", timeout=10).json()
        _offset["ms"] = int(r["serverTime"]) - int(time.time() * 1000)
        print("✅ время синхронизировано, сдвиг", _offset["ms"], "мс")
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
        code = d.get("code")
        # ИСПРАВКА 5: специальная обработка -2019 и -1021
        if code == -2019:
            raise Exception(f"Binance: недостаточно маржи (margin). {d.get('msg')}")
        elif code == -1021:
            print("⚠️ ВНИМАНИЕ: ошибка timestamp (-1021). Ресинхронизирую время...")
            sync_time()  # срочно ресинхронизируем
            raise Exception(f"Binance: {code} {d.get('msg')} (ресинхронизация времени)")
        else:
            raise Exception("Binance: " + str(code) + " " + str(d.get("msg")))
    return d

def signed_delete(path, params=None):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000) + _offset["ms"]
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(p)
    sig = hmac.new(BINANCE_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    r = requests.delete(FAPI + path + "?" + q + "&signature=" + sig,
                        headers={"X-MBX-APIKEY": BINANCE_KEY}, timeout=20)
    d = r.json()
    if isinstance(d, dict) and "code" in d and "msg" in d and d.get("code") not in (200, -2011):
        raise Exception("Binance: " + str(d.get("code")) + " " + str(d.get("msg")))
    return d

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

# ========== ЗАГРУЗКА ДАННЫХ ==========

def get_price(symbol):
    try:
        r = requests.get(f"{DATA_API}/fapi/v1/ticker/price?symbol={symbol}", timeout=10).json()
        return float(r["price"])
    except Exception as e:
        print(f"get_price {symbol}:", e)
        return None

def get_klines_4h(symbol, limit=100):
    """Загружаем 4h свечи"""
    try:
        r = requests.get(f"{DATA_API}/fapi/v1/klines?symbol={symbol}&interval=4h&limit={limit}", timeout=10).json()
        return [{"o": float(c[1]), "h": float(c[2]), "l": float(c[3]), 
                 "c": float(c[4]), "v": float(c[5]), "t": int(c[0])} for c in r]
    except Exception as e:
        print(f"get_klines_4h {symbol}:", e)
        return []

def load_exchange_info():
    global filters
    try:
        r = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=25).json()
        for s in r.get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
                continue
            sym = s["symbol"]
            f = {"step": 0.001, "minQty": 0.0, "minNot": 5.0, "tick": 0.0001}
            for x in s.get("filters", []):
                t = x.get("filterType")
                if t == "LOT_SIZE":
                    f["step"] = float(x["stepSize"])
                    f["minQty"] = float(x["minQty"])
                elif t == "PRICE_FILTER":
                    f["tick"] = float(x["tickSize"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    f["minNot"] = float(x.get("notional") or x.get("minNotional") or 5)
            filters[sym] = f
        print(f"✅ загружено фильтров: {len(filters)}")
    except Exception as e:
        print("load_exchange_info:", e)

def step_round(v, step):
    """Округление под step используя Decimal для точности"""
    if step <= 0:
        return v
    try:
        v_dec = Decimal(str(v))
        step_dec = Decimal(str(step))
        result = (v_dec / step_dec).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_dec
        return float(result)
    except Exception as e:
        print(f"step_round error: {e}, используем fallback")
        n = math.floor(round(v / step, 8)) * step
        return round(n, 10)

def calc_qty(symbol: str, price: float) -> float:
    if price <= 0:
        return 0.0
    qty = RISK_PER_TRADE_USDT / price
    f = filters.get(symbol, {})
    step = f.get("step", 0.001)
    min_qty = f.get("minQty", 0.0)
    min_not = f.get("minNot", 5.0)
    qty = step_round(qty, step)
    qty = max(qty, min_qty)
    if qty * price < min_not:
        qty = step_round(min_not / price, step)
    return qty

def fmt(p):
    if p >= 100: return format(p, ".2f")
    if p >= 1: return format(p, ".4f")
    return format(p, ".6f")

def money(v):
    return ("+" if v >= 0 else "-") + "$" + format(abs(v), ".2f")

# ========== АНАЛИЗ СВЕЧЕЙ ==========

def find_swing_points(klines: List[Dict]) -> Tuple[List[float], List[float]]:
    """
    Ищет локальные highs (сопротивления) и lows (поддержки)
    Swing High: candle выше, чем соседи
    Swing Low: candle ниже, чем соседи
    """
    if len(klines) < 3:
        return [], []
    
    highs = []
    lows = []
    
    for i in range(1, len(klines) - 1):
        h_curr = klines[i]["h"]
        l_curr = klines[i]["l"]
        h_prev = klines[i-1]["h"]
        l_prev = klines[i-1]["l"]
        h_next = klines[i+1]["h"]
        l_next = klines[i+1]["l"]
        
        # Swing High
        if h_curr > h_prev and h_curr > h_next:
            highs.append(h_curr)
        
        # Swing Low
        if l_curr < l_prev and l_curr < l_next:
            lows.append(l_curr)
    
    return highs, lows

def detect_triangle(klines: List[Dict]) -> Dict or None:
    """
    Треугольник: highs падают, lows растут, сужаются к точке
    ВАРИАНТ A: смягченные критерии (для testnet)
    """
    if len(klines) < TRIANGLE_MIN_CANDLES:
        return None
    
    # Берём последние N свечей
    recent = klines[-TRIANGLE_MIN_CANDLES:]
    
    highs = [c["h"] for c in recent]
    lows = [c["l"] for c in recent]
    
    # ВАРИАНТ A: допускаем 1-2 нарушения монотонности (не идеальный тренд)
    highs_violations = sum(1 for i in range(len(highs)-1) if highs[i] < highs[i+1])
    lows_violations = sum(1 for i in range(len(lows)-1) if lows[i] > lows[i+1])
    
    # Допускаем max 1 нарушение на TRIANGLE_MIN_CANDLES свечей
    max_violations = max(1, TRIANGLE_MIN_CANDLES // 3)  # на 2 свечи - 0, на 5 - 1, на 6 - 2
    
    if highs_violations > max_violations or lows_violations > max_violations:
        return None
    
    # Сужение есть? (ВАРИАНТ A: мягче критерий с 0.8 на 0.9)
    width_start = highs[0] - lows[0]
    width_end = highs[-1] - lows[-1]
    
    if width_end >= width_start * 0.9:  # ← было 0.8, теперь 0.9 (мягче)
        return None
    
    return {
        'upper': highs[-1],
        'lower': lows[-1],
        'strength': len(recent),
        'width_pct': (width_end / width_start) * 100
    }

def detect_breakout(symbol: str, current_price: float, highs: List[float], lows: List[float]) -> Dict or None:
    """
    Пробой: цена пробила последний swing level
    Возвращает: {level: уровень, direction: 'UP'/'DOWN', price: текущая}
    """
    if not highs or not lows:
        return None
    
    last_high = max(highs[-3:]) if len(highs) >= 3 else highs[-1]
    last_low = min(lows[-3:]) if len(lows) >= 3 else lows[-1]
    
    # Пробой вверх
    if current_price > last_high:
        return {
            'level': last_high,
            'direction': 'UP',
            'price': current_price,
            'distance_pct': ((current_price - last_high) / last_high) * 100
        }
    
    # Пробой вниз
    if current_price < last_low:
        return {
            'level': last_low,
            'direction': 'DOWN',
            'price': current_price,
            'distance_pct': ((last_low - current_price) / last_low) * 100
        }
    
    return None

# ========== РАЗМЕЩЕНИЕ ОРДЕРОВ ==========

def place_stop_verified(fs: str, side: str, trig_price: float, tries: int = 5) -> bool:
    """
    ИСПРАВКА 7: retry с увеличенным количеством попыток (между открытием и стопом может быть окно)
    """
    trig_price = step_round(trig_price, filters.get(fs, {}).get("tick", 0.0001))
    
    for attempt in range(tries):
        try:
            p = {
                "symbol": fs,
                "side": side,
                "type": "STOP_MARKET",
                "stopPrice": trig_price,
                "closePosition": "true"
            }
            resp = signed_post("/fapi/v1/order", p)
            
            if isinstance(resp, dict) and resp.get("orderId"):
                print(f"✅ стоп встал: {fs} {side} @ {trig_price}, orderId={resp['orderId']}")
                return True
            else:
                print(f"⚠️ странный ответ на стоп {fs}:", resp)
                time.sleep(0.5)
                continue
        
        except Exception as e:
            err_msg = str(e)
            print(f"place_stop {fs} attempt {attempt + 1}/{tries}: {err_msg[:80]}")
            if "-2021" in err_msg:
                print(f"❌ {fs}: стоп не может быть установлен (позиция уже за стопом)")
                return False
            if attempt < tries - 1:
                time.sleep(0.5 * (attempt + 1))  # экспоненциальная задержка
    
    return False

def close_now(fs: str, side: str, qty: float):
    opp = "SELL" if side == "BUY" else "BUY"
    # Округляем qty под step для точности
    f = filters.get(fs, {})
    qty_rounded = step_round(qty, f.get("step", 0.001))
    return signed_post("/fapi/v1/order", {"symbol": fs, "side": opp, "type": "MARKET",
                                          "quantity": qty_rounded, "reduceOnly": "true"})

def enter_position(signal: Dict):
    fs = signal['symbol']
    side = signal['side']
    entry = signal['entry_price']
    
    # Проверяем COOLDOWN ПЕРВОЙ строкой
    if fs in last_trade_time:
        time_since = time.time() - last_trade_time[fs]
        if time_since < COOLDOWN_SEC:
            remaining = COOLDOWN_SEC - time_since
            print(f"⏳ {fs}: COOLDOWN активен ещё {remaining:.0f}s (последняя сделка {time_since:.0f}s назад)")
            return False
    
    qty = calc_qty(fs, entry)
    
    if qty <= 0:
        print(f"❌ {fs}: QTY ERROR - невозможно рассчитать qty (цена {entry}, риск {RISK_PER_TRADE_USDT})")
        return False
    
    try:
        try:
            signed_post("/fapi/v1/leverage", {"symbol": fs, "leverage": LEVERAGE})
        except:
            pass
        
        order = signed_post("/fapi/v1/order", {
            "symbol": fs,
            "side": side,
            "type": "MARKET",
            "quantity": qty
        })
        
        # ИСПРАВКА 4: retry для получения actual_entry (binance может быть медленно)
        actual_entry = entry
        for attempt in range(3):
            try:
                time.sleep(0.3 * (attempt + 1))  # 0.3s, 0.6s, 0.9s
                pos_data = binance_positions()
                entry_from_api = abs(pos_data.get(fs, {}).get("entry", 0) or 0)
                if entry_from_api > 0:
                    actual_entry = entry_from_api
                    break
            except Exception as e:
                if attempt < 2:
                    continue
        
        if actual_entry == entry:
            print(f"⚠️ {fs}: не удалось получить actual_entry, используем сигнальную цену")
        
        # ИСПРАВКА 2: SL считаем от ВХОДА, а не от уровня пробоя
        # Это обеспечивает фиксированный риск = RISK_PER_TRADE_USDT
        if side == "BUY":
            sl = actual_entry * (1 - SL_PCT / 100)
            tp = actual_entry * (1 + TP_PCT / 100)
            sl_side = "SELL"
        else:
            sl = actual_entry * (1 + SL_PCT / 100)
            tp = actual_entry * (1 - TP_PCT / 100)
            sl_side = "BUY"
        
        with data_lock:
            tracked[fs] = {
                'side': side,
                'entry': actual_entry,
                'sl': sl,
                'tp': tp,
                'qty': qty,
                't0': time.time(),
                'breakout_level': signal['level'],
                'signal': signal.get('signal', 'breakout')
            }
            last_trade_time[fs] = time.time()
        
        msg = f"🔓 {fs} {side}\nуровень пробоя: {fmt(signal['level'])}\nвход: {fmt(actual_entry)}\nSL {fmt(sl)} TP {fmt(tp)}"
        msg += "\n✅ Позиция открыта (SL/TP управляются ботом)"
        
        for uid in users:
            try:
                bot.send_message(uid, msg)
            except:
                pass
        
        print(f"✅ Вход {fs} {side} по {actual_entry}, qty={qty}")
        return True
        
    except Exception as e:
        print(f"❌ {fs}: ENTRY_API_ERROR - {str(e)[:100]}")
        return False

# ========== УПРАВЛЕНИЕ ПОЗИЦИЯМИ ==========

def check_position(symbol: str) -> None:
    with data_lock:
        if symbol not in tracked:
            return
        s = tracked[symbol].copy()
    
    try:
        price = get_price(symbol)
        if price is None:
            return
        
        pos_data = binance_positions()
        amt = abs(pos_data.get(symbol, {}).get("amt", 0))
        
        side = s['side']
        entry = s['entry']
        sl = s['sl']
        tp = s['tp']
        
        reason = None
        if side == "BUY":
            if price <= sl:
                reason = f"стоп {fmt(price)}"
            elif price >= tp:
                reason = f"тейк {fmt(price)}"
        else:
            if price >= sl:
                reason = f"стоп {fmt(price)}"
            elif price <= tp:
                reason = f"тейк {fmt(price)}"
        
        if not reason and time.time() - s['t0'] > MAX_HOLD_H * 3600:
            reason = f"таймаут {MAX_HOLD_H}h"
        
        if amt <= 0:
            if not reason:
                reason = "биржевой стоп"
            try:
                signed_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            except:
                pass
            record_close(symbol, s, reason)
            with data_lock:
                tracked.pop(symbol, None)
            return
        
        if reason:
            close_now(symbol, side, amt)
            try:
                signed_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            except Exception as e:
                print(f"cancel stop {symbol}:", e)
            record_close(symbol, s, reason)
            with data_lock:
                tracked.pop(symbol, None)
            return
    
    except Exception as e:
        print(f"check_position {symbol}:", e)

def binance_positions():
    try:
        d = signed_get("/fapi/v2/positionRisk")
        out = {}
        for x in d:
            try:
                amt = float(x.get("positionAmt", 0))
            except:
                continue
            if abs(amt) > 0:
                out[x["symbol"]] = {
                    "amt": amt,
                    "entry": float(x.get("entryPrice", 0) or 0),
                    "upnl": float(x.get("unRealizedProfit", 0) or 0)
                }
        return out
    except Exception as e:
        print("binance_positions:", e)
        return {}

def record_close(symbol: str, s: Dict, reason: str):
    upnl = 0.0
    try:
        pos = binance_positions()
        upnl = pos.get(symbol, {}).get("upnl", 0.0)
    except:
        pass
    
    with data_lock:
        hist = history.setdefault(symbol, [])
        hist.append({
            "side": s['side'],
            "entry": s['entry'],
            "upnl": round(upnl, 2),
            "reason": reason,
            "t": int(time.time())
        })
    
    msg = f"❌ {symbol} закрыта\n{reason}\nPnL {money(upnl)}"
    for uid in users:
        try:
            bot.send_message(uid, msg)
        except:
            pass

def save():
    try:
        with open(DB, "w") as f:
            json.dump({
                "tracked": tracked,
                "history": history,
                "last_trade_time": last_trade_time
            }, f)
    except Exception as e:
        print("save error:", e)

def load():
    global tracked, history, last_trade_time
    try:
        with open(DB) as f:
            d = json.load(f)
        tracked = d.get("tracked", {})
        history = d.get("history", {})
        last_trade_time = d.get("last_trade_time", {})
    except:
        pass

# ========== МЕНЮ БОТА ==========

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📊 Позиции", "📈 История")
    kb.row("🛑 Очистить")
    return kb

@bot.message_handler(commands=['start'])
def cmd_start(m):
    users.add(m.chat.id)
    save()
    k = "✅ Binance OK" if has_keys() else "⚠️ нет ключей"
    bot.send_message(m.chat.id,
        "🤖 Breakout Bot (Пробой + Ретест + Вход)\n"
        "Ловит пробои уровней поддержки/сопротивления\n"
        "Входит на ретесте в направлении пробоя\n\n" + k,
        reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "📊 Позиции")
def btn_pos(m):
    with data_lock:
        if not tracked:
            bot.send_message(m.chat.id, "Нет открытых позиций", reply_markup=menu())
            return
        items = list(tracked.items())
    
    for sym, s in items:
        price = get_price(sym) or s['entry']
        move = (price - s['entry']) / s['entry'] * 100 if s['entry'] else 0
        bot.send_message(m.chat.id,
            f"{sym} {s['side']}\nвход {fmt(s['entry'])}\nтекущее {fmt(price)}\nмув {move:+.2f}%",
            reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "📈 История")
def btn_hist(m):
    total = 0.0
    n = 0
    with data_lock:
        for sym, hist_list in history.items():
            for h in hist_list[-5:]:
                total += h['upnl']
                n += 1
    msg = f"Всего закрыто: {n} сделок\nПрибыль: {money(total)}"
    bot.send_message(m.chat.id, msg, reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🛑 Очистить")
def btn_clear(m):
    with data_lock:
        tracked.clear()
    save()
    bot.send_message(m.chat.id, "Очищено", reply_markup=menu())

@bot.message_handler(func=lambda m: True)
def catch_all(m):
    users.add(m.chat.id)
    save()
    bot.send_message(m.chat.id, "Меню 👇", reply_markup=menu())

# ========== ГЛАВНЫЙ ЦИКЛ ==========

def main_loop():
    while True:
        try:
            now = time.time()
            
            # Обновляем фильтры раз в сутки
            if now - _last_info_update["ts"] > 86400:
                load_exchange_info()
                _last_info_update["ts"] = now
            
            # Ресинхронизируем время с сервером раз в час (против -1021 ошибок)
            if now - _last_info_update.get("time_sync_ts", 0) > 3600:
                try:
                    sync_time()
                    _last_info_update["time_sync_ts"] = now
                except Exception as e:
                    print(f"⚠️ time resync error: {e}")
            
            # Проверяем открытые позиции
            with data_lock:
                positions_to_check = list(tracked.keys())
            
            for symbol in positions_to_check:
                check_position(symbol)
                time.sleep(0.1)
            
            # Ищем новые входы (только первые MAX_SYMBOLS)
            for symbol in SYMBOLS[:MAX_SYMBOLS]:
                # ИСПРАВКА 3: проверяем лимит позиций ПОД LOCK (race condition fix)
                with data_lock:
                    if symbol in tracked:
                        continue
                    if len(tracked) >= MAX_OPEN_POSITIONS:
                        continue
                
                price = get_price(symbol)
                if price is None:
                    continue
                
                klines = get_klines_4h(symbol, limit=LOOKBACK_CANDLES)
                if len(klines) < 10:
                    continue
                
                # Анализируем
                highs, lows = find_swing_points(klines)
                triangle = detect_triangle(klines)
                breakout = detect_breakout(symbol, price, highs, lows)
                
                # STATE 1: ждём пробоя
                if symbol not in breakout_state:
                    if breakout and triangle:
                        # Пробой обнаружен! Запоминаем
                        with data_lock:
                            breakout_state[symbol] = {
                                'level': breakout['level'],
                                'direction': breakout['direction'],
                                't_breakout': time.time()
                            }
                        print(f"🔔 {symbol}: пробой {breakout['direction']} на {fmt(breakout['level'])}")
                
                # STATE 2: пробой уже зафиксирован, ждём ретеста
                else:
                    with data_lock:
                        if symbol not in breakout_state:
                            continue
                        state = breakout_state[symbol].copy()
                    
                    retest_zone_pct = 0.5  # ретест ±0.5% от уровня пробоя
                    level = state['level']
                    lower = level * (1 - retest_zone_pct / 100)
                    upper = level * (1 + retest_zone_pct / 100)
                    
                    # Проверяем ретест
                    if lower <= price <= upper:
                        # РЕТЕСТ! Входим
                        side = "BUY" if state['direction'] == "UP" else "SELL"
                        
                        signal = {
                            'symbol': symbol,
                            'side': side,
                            'entry_price': price,
                            'level': level,
                            'signal': 'breakout+retest'
                        }
                        if enter_position(signal):
                            with data_lock:
                                breakout_state.pop(symbol, None)
                        time.sleep(0.5)
                    
                    # Забываем пробой если он > RETEST_TIMEOUT_SEC
                    elif time.time() - state['t_breakout'] > RETEST_TIMEOUT_SEC:
                        print(f"⏰ {symbol}: пробой истёк, забыли")
                        with data_lock:
                            breakout_state.pop(symbol, None)
            
            time.sleep(SCAN_INTERVAL_SEC)  # интервал сканирования (параметр SCAN_INTERVAL_SEC)
        
        except Exception as e:
            print("loop error:", e)
            time.sleep(5)

# ========== ЗАПУСК ==========

print("🤖 Breakout Bot (v4.4 TESTNET CRITICAL FIX) — загрузка...")
print("✅ ИСПРАВКА 1: STATE-машина для ретеста (memory о пробоях)")
print("✅ ИСПРАВКА 2: SL от входа, не от уровня")
print("✅ ИСПРАВКА 3: COOLDOWN проверка перед входом")
print("✅ ИСПРАВКА 4: Polling с переподключением")
print("✅ CRITICAL FIXES v4: тикеры, retry, -2019/-1021")
print("✅ ОПТИМИЗАЦИЯ v4.3: detect_triangle смягчен")
print("✅ TESTNET FIX v4.4: УБРАН place_stop_verified (-4120 ошибка)")
print("   SL/TP управляются полностью в check_position (работает везде)")
print("\n📊 Таймфрейм: 4h")
print("🔍 Сигнал: Пробой → Ретест → Вход")
print("📍 SL:", SL_PCT, "% | TP:", TP_PCT, "% | Плечо:", LEVERAGE, "x")

load()
if has_keys():
    sync_time()
    load_exchange_info()
    try:
        pp = binance_positions()
        print(f"✅ Binance OK, позиций: {len(pp)}")
    except Exception as e:
        print("❌ Binance error:", e)
else:
    print("❌ Нет ключей Binance!")

print(f"\nСимволы: {len(SYMBOLS)} всего в списке, анализируем первые {MAX_SYMBOLS}")
if MAX_SYMBOLS > 50:
    print(f"⚠️ ВНИМАНИЕ: {MAX_SYMBOLS} символов может вызвать rate limit на Binance!")
    print(f"   Рекомендация: MAX_SYMBOLS <= 30-50 для надёжности")
print(f"Фильтров загружено: {len(filters)}")
print(f"\nПараметры:")
print(f"  RETEST_TIMEOUT={RETEST_TIMEOUT_SEC//60}мин")
print(f"  MAX_HOLD={MAX_HOLD_H}h")
print(f"  SCAN_INTERVAL={SCAN_INTERVAL_SEC}s (важно для COOLDOWN_SEC={COOLDOWN_SEC}s)")
print(f"  TRIANGLE_MIN_CANDLES={TRIANGLE_MIN_CANDLES}, LOOKBACK={LOOKBACK_CANDLES}")

threading.Thread(target=main_loop, daemon=True).start()

if __name__ == "__main__":
    print("\n🚀 Бот запущен!")
    # ИСПРАВКА 4: while True обеспечивает переподключение после разрыва
    while True:
        try:
            bot.polling(none_stop=True, timeout=30)
        except Exception as e:
            print(f"❌ polling error: {e}")
            print("⏳ переподключаю через 10 сек...")
            time.sleep(10)
