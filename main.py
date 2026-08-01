import os
import json
import time
import hmac
import hashlib
import urllib.parse
import math
import threading
from datetime import datetime, timedelta, timezone
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
DATA_API = os.environ.get("DATA_API", FAPI).rstrip("/")  # ← КЛЮЧ: ТОЖЕ контур, что FAPI!

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",")
SYMBOLS = [s.strip() for s in SYMBOLS if s.strip()]

MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "100.0"))
DENSITY_CLUSTER_PCT = float(os.environ.get("DENSITY_CLUSTER_PCT", "0.05"))
ENTRY_RANGE_PCT = float(os.environ.get("ENTRY_RANGE_PCT", "0.02"))
RISK_PER_TRADE_USDT = float(os.environ.get("RISK_PER_TRADE_USDT", "50.0"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
LEVERAGE = int(os.environ.get("LEVERAGE", "5"))
SL_PCT = float(os.environ.get("SL_PCT", "0.5"))
TP_PCT = float(os.environ.get("TP_PCT", "1.0"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "60"))

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
data_lock = threading.Lock()  # ← ПРАВКА 5: Lock для синхронизации потоков
_offset = {"ms": 0}
_last_info_update = {"ts": 0}  # ПРАВКА 3: отслеживаем последнее обновление фильтров

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
        raise Exception("Binance: " + str(d.get("code")) + " " + str(d.get("msg")))
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

# ========== ПРАВКА 3: Данные с ТОГО ЖЕ контура, что FAPI ==========

def get_price(symbol):
    try:
        r = requests.get(f"{DATA_API}/fapi/v1/ticker/price?symbol={symbol}", timeout=10).json()
        return float(r["price"])
    except Exception as e:
        print(f"get_price {symbol}:", e)
        return None

def get_depth(symbol, limit=50):
    try:
        r = requests.get(f"{DATA_API}/fapi/v1/depth?symbol={symbol}&limit={limit}", timeout=10).json()
        bids = [[float(x[0]), float(x[1])] for x in r.get("bids", [])]
        asks = [[float(x[0]), float(x[1])] for x in r.get("asks", [])]
        return bids, asks
    except Exception as e:
        print(f"get_depth {symbol}:", e)
        return [], []

def get_klines_1h(symbol, limit=24):
    try:
        r = requests.get(f"{DATA_API}/fapi/v1/klines?symbol={symbol}&interval=1h&limit={limit}", timeout=10).json()
        return [{"c": float(c[4]), "v": float(c[7]), "t": int(c[6])} for c in r]
    except Exception:
        return []

# ========== ПРАВКА 4: Загрузить фильтры и округление ==========

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
    if step <= 0:
        return v
    n = math.floor(round(v / step, 8)) * step
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n, d)

def calc_qty(symbol: str, price: float) -> float:
    """
    Рассчитать qty под фильтры Binance.
    ПРАВКА 4: учитываем stepSize, minQty, minNotional
    """
    if price <= 0:
        return 0.0
    
    qty = RISK_PER_TRADE_USDT / price
    
    f = filters.get(symbol, {})
    step = f.get("step", 0.001)
    min_qty = f.get("minQty", 0.0)
    min_not = f.get("minNot", 5.0)
    
    # Округляем под stepSize
    qty = step_round(qty, step)
    qty = max(qty, min_qty)
    
    # Проверяем minNotional
    if qty * price < min_not:
        qty = step_round(min_not / price, step)
    
    return qty

def fmt(p):
    if p >= 100: return format(p, ".2f")
    if p >= 1: return format(p, ".4f")
    return format(p, ".6f")

def money(v):
    return ("+" if v >= 0 else "-") + "$" + format(abs(v), ".2f")

# ========== ПОИСК ПЛОТНОСТЕЙ ==========

def find_density_clusters(bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> Dict[str, Dict]:
    clusters = {}
    now = time.time()
    
    # Биды
    for i, (price, size) in enumerate(bids[:15]):
        vol_usd = price * size
        if vol_usd < MIN_VOLUME_USD:
            continue
        
        cluster_vol = vol_usd
        cluster_prices = [price]
        
        for j in range(i + 1, min(i + 5, len(bids))):
            neighbor_price, neighbor_size = bids[j]
            if neighbor_price / price < (1 - DENSITY_CLUSTER_PCT / 100):
                break
            cluster_vol += neighbor_price * neighbor_size
            cluster_prices.append(neighbor_price)
        
        if cluster_vol >= MIN_VOLUME_USD * 1.5:
            avg_price = np.mean(cluster_prices)
            key = f"bid_{avg_price:.4f}"
            if key not in clusters:
                clusters[key] = {
                    'side': 'BID',
                    'price': avg_price,
                    'vol_usd': cluster_vol,
                    'size': sum(s for _, s in bids[i:min(i+len(cluster_prices), len(bids))]),
                    'ts': now
                }
    
    # Аски
    for i, (price, size) in enumerate(asks[:15]):
        vol_usd = price * size
        if vol_usd < MIN_VOLUME_USD:
            continue
        
        cluster_vol = vol_usd
        cluster_prices = [price]
        
        for j in range(i + 1, min(i + 5, len(asks))):
            neighbor_price, neighbor_size = asks[j]
            if neighbor_price / price > (1 + DENSITY_CLUSTER_PCT / 100):
                break
            cluster_vol += neighbor_price * neighbor_size
            cluster_prices.append(neighbor_price)
        
        if cluster_vol >= MIN_VOLUME_USD * 1.5:
            avg_price = np.mean(cluster_prices)
            key = f"ask_{avg_price:.4f}"
            if key not in clusters:
                clusters[key] = {
                    'side': 'ASK',
                    'price': avg_price,
                    'vol_usd': cluster_vol,
                    'size': sum(s for _, s in asks[i:min(i+len(cluster_prices), len(asks))]),
                    'ts': now
                }
    
    return clusters

def get_momentum(symbol) -> float:
    klines = get_klines_1h(symbol, limit=2)
    if len(klines) < 2:
        return 0.0
    prev_close = float(klines[-2]["c"])
    curr_close = float(klines[-1]["c"])
    return (curr_close - prev_close) / prev_close * 100

def decide_entry(symbol: str, price: float, clusters: Dict) -> Dict or None:
    with data_lock:
        if len(tracked) >= MAX_OPEN_POSITIONS:
            return None
        
        if symbol in last_trade_time:
            if time.time() - last_trade_time[symbol] < COOLDOWN_SEC:
                return None
    
    momentum = get_momentum(symbol)
    
    for key, cluster in clusters.items():
        if cluster['side'] == 'BID':
            lower = cluster['price']
            upper = cluster['price'] * (1 + ENTRY_RANGE_PCT / 100)
            if lower <= price < upper and momentum > -0.1:
                return {
                    'symbol': symbol,
                    'side': 'BUY',
                    'entry_price': price,
                    'density_price': cluster['price'],
                    'vol_usd': cluster['vol_usd'],
                    'momentum': momentum
                }
        
        elif cluster['side'] == 'ASK':
            upper = cluster['price']
            lower = cluster['price'] * (1 - ENTRY_RANGE_PCT / 100)
            if lower < price <= upper and momentum < 0.1:
                return {
                    'symbol': symbol,
                    'side': 'SELL',
                    'entry_price': price,
                    'density_price': cluster['price'],
                    'vol_usd': cluster['vol_usd'],
                    'momentum': momentum
                }
    
    return None

# ========== РАЗМЕЩЕНИЕ ОРДЕРОВ ==========

def place_stop_verified(fs: str, side: str, trig_price: float, tries: int = 3) -> bool:
    """
    ПРАВКА 1: используем /fapi/v1/order type=STOP_MARKET stopPrice
    ПРАВКА 6: проверяем РЕАЛЬНЫЙ ответ (orderId)
    """
    trig_price = step_round(trig_price, filters.get(fs, {}).get("tick", 0.0001))
    
    for attempt in range(tries):
        try:
            p = {
                "symbol": fs,
                "side": side,
                "type": "STOP_MARKET",        # ← ПРАВКА 1
                "stopPrice": trig_price,       # ← ПРАВКА 1 (не triggerPrice!)
                "closePosition": "true"
                # БЕЗ timeInForce и БЕЗ reduceOnly — closePosition берёт на себя
            }
            resp = signed_post("/fapi/v1/order", p)  # ← ПРАВКА 1 (не algoOrder!)
            
            # ПРАВКА 6: проверяем РЕАЛЬНЫЙ ответ
            if isinstance(resp, dict) and resp.get("orderId"):
                print(f"✅ стоп встал: {fs} {side} @ {trig_price}, orderId={resp['orderId']}")
                return True
            else:
                print(f"⚠️ странный ответ на стоп {fs}:", resp)
                time.sleep(1)
                continue
        
        except Exception as e:
            err_msg = str(e)
            print(f"place_stop {fs} attempt {attempt + 1}: {err_msg[:80]}")
            if "-2021" in err_msg:
                return False
            time.sleep(1)
    
    return False

def close_now(fs: str, side: str, qty: float):
    opp = "SELL" if side == "BUY" else "BUY"
    return signed_post("/fapi/v1/order", {"symbol": fs, "side": opp, "type": "MARKET",
                                          "quantity": qty, "reduceOnly": "true"})

def enter_position(signal: Dict):
    """
    ПРАВКА 2: убрали timeInForce из MARKET
    ПРАВКА 4: используем новую calc_qty с фильтрами
    ПРАВКА 5: используем data_lock
    """
    fs = signal['symbol']
    side = signal['side']
    entry = signal['entry_price']
    qty = calc_qty(fs, entry)  # ← ПРАВКА 4
    
    if qty <= 0:
        print(f"❌ {fs}: невозможно рассчитать qty (цена {entry}, риск {RISK_PER_TRADE_USDT})")
        return False
    
    try:
        try:
            signed_post("/fapi/v1/leverage", {"symbol": fs, "leverage": LEVERAGE})
        except:
            pass
        
        # ПРАВКА 2: убрали timeInForce
        order = signed_post("/fapi/v1/order", {
            "symbol": fs,
            "side": side,
            "type": "MARKET",
            "quantity": qty
        })
        
        # КРИТИЧНО (пункт 1): fills на фьючерсах часто пуст
        # Берём actual_entry из positionRisk (реальная цена входа)
        time.sleep(0.5)
        try:
            pos_data = binance_positions()
            actual_entry = abs(pos_data.get(fs, {}).get("entry", 0) or entry)
            if actual_entry <= 0:
                actual_entry = entry
        except:
            actual_entry = entry
        
        if side == "BUY":
            sl = actual_entry * (1 - SL_PCT / 100)
            tp = actual_entry * (1 + TP_PCT / 100)
            sl_side = "SELL"
        else:
            sl = actual_entry * (1 + SL_PCT / 100)
            tp = actual_entry * (1 - TP_PCT / 100)
            sl_side = "BUY"
        
        stop_ok = place_stop_verified(fs, sl_side, sl)
        
        # ПРАВКА 5: используем data_lock
        with data_lock:
            tracked[fs] = {
                'side': side,
                'entry': actual_entry,
                'sl': sl,
                'tp': tp,
                'qty': qty,
                't0': time.time(),
                'density_vol': signal.get('vol_usd', 0)
            }
            last_trade_time[fs] = time.time()
        
        msg = f"🎯 {fs} {side}\nвход {fmt(actual_entry)}\nSL {fmt(sl)} TP {fmt(tp)}\nqty {qty}"
        if stop_ok:
            msg += "\n🛡 стоп на бирже ✅"
        else:
            msg += "\n⚠️ СТОП НЕ ВСТАЛ! Закрываю позицию!"
            try:
                close_now(fs, side, qty)
                # ПРАВКА 2: отменяем все ордера по этому символу
                signed_delete("/fapi/v1/allOpenOrders", {"symbol": fs})
            except Exception as e:
                print(f"close after no-stop {fs}:", e)
            with data_lock:
                tracked.pop(fs, None)
            return False
        
        for uid in users:
            try:
                bot.send_message(uid, msg)
            except:
                pass
        
        print(f"✅ Вход {fs} {side} по {actual_entry}, qty={qty}")
        return True
        
    except Exception as e:
        print(f"enter_position {fs}:", e)
        return False

# ========== УПРАВЛЕНИЕ ПОЗИЦИЯМИ ==========

def check_position(symbol: str) -> None:
    """ПРАВКА 5: используем data_lock"""
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
        
        # КРИТИЧНО (пункт 2): сначала проверяем SL/TP, потом смотрим amt <= 0
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
        
        # Таймаут
        if not reason and time.time() - s['t0'] > 1800:
            reason = "таймаут"
        
        # Если позиция уже закрыта
        if amt <= 0:
            if not reason:
                reason = "биржевой стоп"
            # ПРАВКА 2: отменяем ордера (если что-то осталось)
            try:
                signed_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            except:
                pass
            record_close(symbol, s, reason)
            with data_lock:
                tracked.pop(symbol, None)
            return
        
        # Если позиция ещё открыта, проверяем SL/TP и закрываем если нужно
        if reason:
            close_now(symbol, side, amt)
            # ПРАВКА 2: отменяем старый стоп-ордер, чтобы не срабатил на следующей сделке
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
    """ПРАВКА 5: используем data_lock"""
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
        "🤖 Скальпер от плотностей (ВСЕ 6 ПРАВОК!)\n"
        "Ловит скопления ордеров в стакане.\n\n" + k,
        reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "📊 Позиции")
def btn_pos(m):
    with data_lock:  # ← ПРАВКА 5
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
    with data_lock:  # ← ПРАВКА 5
        for sym, hist_list in history.items():
            for h in hist_list[-5:]:
                total += h['upnl']
                n += 1
    msg = f"Всего закрыто: {n} сделок\nПрибыль: {money(total)}"
    bot.send_message(m.chat.id, msg, reply_markup=menu())

@bot.message_handler(func=lambda m: m.text == "🛑 Очистить")
def btn_clear(m):
    with data_lock:  # ← ПРАВКА 5
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
            # ПРАВКА 3: обновляем фильтры раз в сутки
            now = time.time()
            if now - _last_info_update["ts"] > 86400:
                load_exchange_info()
                _last_info_update["ts"] = now
            
            # Проверяем открытые позиции
            with data_lock:  # ← ПРАВКА 5
                positions_to_check = list(tracked.keys())
            
            for symbol in positions_to_check:
                check_position(symbol)
                time.sleep(0.1)
            
            # Ищем новые входы
            for symbol in SYMBOLS:
                with data_lock:  # ← ПРАВКА 5
                    if symbol in tracked:
                        continue
                    pos_count = len(tracked)
                
                if pos_count >= MAX_OPEN_POSITIONS:
                    continue
                
                price = get_price(symbol)
                if price is None:
                    continue
                
                bids, asks = get_depth(symbol, limit=50)
                if not bids or not asks:
                    continue
                
                clusters = find_density_clusters(bids, asks)
                if not clusters:
                    continue
                
                signal = decide_entry(symbol, price, clusters)
                if signal:
                    enter_position(signal)
                    time.sleep(0.5)
            
            time.sleep(2)
        
        except Exception as e:
            print("loop error:", e)
            time.sleep(5)

# ========== ЗАПУСК ==========

print("🤖 Скальпер от плотностей — загрузка...")
print("✅ ПРАВКА 1: /fapi/v1/order + STOP_MARKET + stopPrice (без reduceOnly)")
print("✅ ПРАВКА 2: отменяем старый стоп после ручного закрытия")
print("✅ ПРАВКА 3: DATA_API=" + DATA_API + " | обновляем фильтры раз в сутки")
print("✅ ПРАВКА 4: calc_qty с stepSize/minQty/minNot")
print("✅ ПРАВКА 5: threading.Lock() для tracked/history")
print("✅ ПРАВКА 6: проверяем orderId в ответе Binance")
if "testnet" in FAPI.lower():
    print("\n⚠️ ВНИМАНИЕ: testnet != mainnet")
    print("  • Стакан testnet нестабилен (мало ликвидности)")
    print("  • Кластеры плотностей могут быть нерепрезентативны")
    print("  • Для продакшена используй боевые контура")

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

print(f"\nПараметры:")
print(f"  Риск: ${RISK_PER_TRADE_USDT} | SL {SL_PCT}% | TP {TP_PCT}% | Плечо {LEVERAGE}x")
print(f"  Контур: FAPI={FAPI}")
print(f"  Символы: {', '.join(SYMBOLS)}")
print(f"  Фильтров загружено: {len(filters)}")

threading.Thread(target=main_loop, daemon=True).start()

if __name__ == "__main__":
    print("\n🚀 Бот запущен!")
    try:
        bot.polling(none_stop=True, timeout=30)
    except Exception as e:
        print("polling error:", e)
        time.sleep(5)
