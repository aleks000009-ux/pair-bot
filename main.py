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
STATS_START = os.environ.get("STATS_START", "2026-07-21 00:00").strip()
MSK = timezone(timedelta(hours=3))

# источники рыночных данных: если основной не отвечает — берём следующий
DATA_HOSTS = [h.strip().rstrip("/") for h in os.environ.get(
    "DATA_HOSTS",
    "https://data-api.binance.vision,https://api.binance.com,"
    "https://api1.binance.com,https://api2.binance.com,https://fapi.binance.com"
).split(",") if h.strip()]
_host_idx = {"i": 0}

SIZE = 1000                                             # номинал позиции
# --- ФАНДИНГ-ФАРМ соло (без хеджа, защита ATR-стопом) ---
MIN_FUNDING = float(os.environ.get("MIN_FUNDING", "0.5"))   # мин. |фандинг| за 8ч-эквивалент, %
EXIT_FUNDING = float(os.environ.get("EXIT_FUNDING", "0.2")) # выход когда |фандинг| упал ниже, %
ATR_STOP = float(os.environ.get("ATR_STOP", "2.5"))         # стоп = ATR_STOP × ATR(1ч)
STOP_MIN = float(os.environ.get("STOP_MIN", "4.0"))         # но не ближе этого %, % от входа
STOP_MAX = float(os.environ.get("STOP_MAX", "8.0"))         # и не дальше этого %
MAX_IMPULSE = float(os.environ.get("MAX_IMPULSE", "20.0"))  # не входим если цена сдвинулась >этого % за IMPULSE_H
IMPULSE_H = int(os.environ.get("IMPULSE_H", "12"))          # окно проверки импульса, часов
MAX_ATR_RATIO = float(os.environ.get("MAX_ATR_RATIO", "3.0")) # пропускаем если ATR сейчас > этого × среднего ATR
TRAIL_CALLBACK = float(os.environ.get("TRAIL_CALLBACK", "2.0"))  # отступ трейлинга, % (биржевой callbackRate)
TRAIL_ACTIVATE = float(os.environ.get("TRAIL_ACTIVATE", "3.0"))  # активация трейлинга при +этого % от входа
TRAIL_START = float(os.environ.get("TRAIL_START", "4.0"))   # (устар., прогр. трейлинг больше не используется)
TRAIL_GAP = float(os.environ.get("TRAIL_GAP", "2.0"))       # (устар.)
TF = os.environ.get("PAIR_TF", "1h")                        # ТФ для ATR
CAND_COINS = int(os.environ.get("CAND_COINS", "80"))        # из скольких монет ищем фандинг
MAX_HOLD_H = float(os.environ.get("MAX_HOLD_H", "168"))     # макс. удержание, часов
MAX_RISK = float(os.environ.get("MAX_RISK", "3.0"))
# заглушки под переиспользуемый код
MIN_CORR = LEG_STOP = 0.0
ZWIN = int(os.environ.get("ZWIN", "100"))
CORR_WIN = int(os.environ.get("CORR_WIN", "200"))
Z_ENTRY = Z_EXIT = Z_STOP = 0.0
FEE = 0.055
BE_BAND = 5.0
BTC_FLAT = 0.7
BTC_FILTER = False
SCAN_EVERY = int(os.environ.get("SCAN_EVERY", "600"))
ZWIN = int(os.environ.get("ZWIN", "100"))   # окно для corr-расчётов

# мусорные/тестовые монеты — не торгуем и не показываем
BLACKLIST = set(x.strip().upper() for x in os.environ.get(
    "BLACKLIST", "币安人生,BINANCELIFE").split(",") if x.strip())

# статистику по этим символам игнорируем (случайные ручные сделки)
STATS_IGNORE = set(x.strip().upper() for x in os.environ.get(
    "STATS_IGNORE", "BTCUSDT,ETHUSDT").split(",") if x.strip())

# ---------- АВТОТОРГОВЛЯ ----------
AUTO_TRADE = os.environ.get("AUTO_TRADE", "1") == "1"   # по умолчанию ВКЛ, чтобы не гасла после деплоя
AUTO_MIN_PTS = int(os.environ.get("AUTO_MIN_PTS", "4"))
MAX_POS = int(os.environ.get("MAX_POS", "6"))
LEVERAGE = int(os.environ.get("LEVERAGE", "5"))
auto_on = AUTO_TRADE
# кому слать автосканы. Задай CHAT_ID в Railway — иначе список теряется при редеплое
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
opened_keys = set()

DATA_DIR = os.environ.get("DATA_DIR", ".").rstrip("/")
DB = DATA_DIR + "/trades.json"

tracked = {}
users = set()
if CHAT_ID:
    try:
        users.add(int(CHAT_ID))
    except Exception:
        print("CHAT_ID неверный:", CHAT_ID)
sent_signals = {}
_last_quiet = {}
funnel = {}
history = {}


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


_filters = {}


def load_filters():
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
    s = sym + "USDT"
    return s if s in _filters else None


def place_cond(fs, side, typ, trig):
    """условный ордер через новый algoOrder (обязателен с 09.12.2025)"""
    p = {"algoType": "CONDITIONAL", "symbol": fs, "side": side, "type": typ,
         "triggerPrice": trig, "closePosition": "true",
         "workingType": "MARK_PRICE", "timeInForce": "GTE_GTC"}
    return signed_post("/fapi/v1/algoOrder", p)


def cancel_algo(fs):
    """снять все условные ордера по символу — Binance ждёт DELETE, не POST"""
    ok = False
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
        try:
            signed_delete(path, {"symbol": fs})
            ok = True
        except Exception as e:
            print("cancel_algo", path, ":", str(e)[:90])
    return ok


def algo_orders_count(fs):
    """сколько условных ордеров реально стоит на бирже по символу"""
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/openOrders"):
        try:
            r = signed_get(path, {"symbol": fs})
            if isinstance(r, list):
                return len(r)
            if isinstance(r, dict) and "orders" in r:
                return len(r["orders"])
        except Exception as e:
            print("algo count", path, ":", str(e)[:60])
    return -1        # не смогли проверить


def place_stop_verified(fs, side, trig, tries=3):
    """
    Ставим защитный стоп и ПРОВЕРЯЕМ, что он реально появился на бирже.
    До tries попыток. Возвращает True только если ордер подтверждён.
    """
    for attempt in range(tries):
        try:
            place_cond(fs, side, "STOP_MARKET", trig)
        except Exception as e:
            print("place_stop", fs, "attempt", attempt + 1, ":", str(e)[:80])
            time.sleep(1)
            continue
        time.sleep(1)        # даём бирже записать ордер
        n = algo_orders_count(fs)
        if n > 0:
            return True      # ордер подтверждён на бирже
        if n == 0:
            # ордер не появился — пробуем ещё раз
            print("stop не подтверждён на бирже, попытка", attempt + 1)
            time.sleep(1)
            continue
        # n == -1: проверить не смогли, считаем что поставили (не блокируем)
        return True
    return False


def place_trailing(fs, side, activate_price, callback_rate, qty):
    """
    Биржевой трейлинг-стоп через algoOrder.
    side — сторона ЗАКРЫТИЯ (для лонга SELL, для шорта BUY).
    activate_price — цена активации (для SELL выше текущей, для BUY ниже).
    callback_rate — отступ в % (1.0 = 1%).
    Биржа сама тянет стоп за ценой — надёжнее программного.
    """
    p = {"algoType": "CONDITIONAL", "symbol": fs, "side": side,
         "type": "TRAILING_STOP_MARKET",
         "activatePrice": activate_price, "callbackRate": callback_rate,
         "quantity": qty, "reduceOnly": "true",
         "workingType": "CONTRACT_PRICE"}
    return signed_post("/fapi/v1/algoOrder", p)


def place_trailing_verified(fs, side, activate_price, callback_rate, qty, tries=3):
    """ставим трейлинг и проверяем, что он появился на бирже"""
    for attempt in range(tries):
        try:
            place_trailing(fs, side, activate_price, callback_rate, qty)
        except Exception as e:
            msg = str(e)
            print("trailing", fs, "attempt", attempt + 1, ":", msg[:90])
            # -2021 = activation price мимо, пересчитывать бессмысленно повтором
            if "-2021" in msg:
                return False
            time.sleep(1)
            continue
        time.sleep(1)
        n = algo_orders_count(fs)
        if n > 0:
            return True
        if n == 0:
            time.sleep(1)
            continue
        return True      # проверить не смогли — считаем ок
    return False


def close_now(fs, side, qty):
    opp = "SELL" if side == "BUY" else "BUY"
    return signed_post("/fapi/v1/order", {"symbol": fs, "side": opp, "type": "MARKET",
                                          "quantity": qty, "reduceOnly": "true"})


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


def algo_open_orders():
    """открытые условные ордера -> {symbol: {'tp':цена,'sl':цена}}"""
    out = {}
    for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/openOrders"):
        try:
            d = signed_get(path)
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for o in d:
            sym = o.get("symbol")
            if not sym:
                continue
            typ = (o.get("type") or o.get("algoType") or "").upper()
            trg = o.get("triggerPrice") or o.get("stopPrice") or 0
            try:
                trg = float(trg)
            except Exception:
                continue
            if trg <= 0:
                continue
            out.setdefault(sym, {})
            if "TAKE_PROFIT" in typ:
                out[sym]["tp"] = trg
            elif "STOP" in typ:
                out[sym]["sl"] = trg
        if out:
            break
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
    out = []
    week = 7 * 86400 * 1000
    cur = int(start_ms)
    end = int(time.time() * 1000) + 60000
    guard = 0
    while cur < end and guard < 60:
        guard += 1
        stop = min(cur + week, end)
        # пагинация ВНУТРИ недели: если вернулось ровно 1000 (лимит) —
        # значит записей больше, тянем дальше от времени последней
        sub = cur
        inner = 0
        while inner < 20:
            inner += 1
            try:
                rows = binance_income(sub, symbol, stop)
            except Exception as e:
                print("income chunk error:", e)
                break
            if not rows:
                break
            out.extend(rows)
            if len(rows) < 1000:
                break                     # неделя вычерпана
            # сдвигаемся к времени последней записи +1мс
            last_t = max(int(x.get("time", sub)) for x in rows)
            if last_t <= sub:
                break
            sub = last_t + 1
            time.sleep(0.15)
        cur = stop
        time.sleep(0.2)
    # дедуп по (time, incomeType, symbol, income) — пагинация может дать нахлёст
    seen = set()
    uniq = []
    for x in out:
        k = (x.get("time"), x.get("incomeType"), x.get("symbol"), x.get("income"), x.get("tranId"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


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


def group_trades(rows):
    """
    Разбивает поток income на ОТДЕЛЬНЫЕ СДЕЛКИ.
    Раньше группировка шла по монете: три входа в SOL считались за одну сделку.
    Теперь граница сделки — момент закрытия (кластер записей REALIZED_PNL).
    Комиссии и фандинг относим к тому закрытию, которое идёт после них.
    """
    CLUSTER_SEC = 120          # два филла одного выхода = одна сделка
    by_sym = {}
    for x in rows:
        sym = x.get("symbol") or "—"
        try:
            x["_t"] = int(x.get("time", 0))
            x["_v"] = float(x.get("income", 0))
        except Exception:
            continue
        by_sym.setdefault(sym, []).append(x)

    trades = []
    for sym, lst in by_sym.items():
        lst.sort(key=lambda z: z["_t"])
        # 1) границы закрытий
        closes = []
        for x in lst:
            if x.get("incomeType") != "REALIZED_PNL":
                continue
            t = x["_t"]
            if closes and t - closes[-1][1] <= CLUSTER_SEC * 1000:
                closes[-1][1] = t
            else:
                closes.append([t, t])
        if not closes:
            continue
        # 2) раскидываем записи по закрытиям
        buckets = [{"sym": sym, "t": c[1], "pnl": 0.0, "fee": 0.0,
                    "fund": 0.0, "other": 0.0, "n": 0} for c in closes]
        for x in lst:
            t = x["_t"]
            idx = None
            for i, c in enumerate(closes):
                if t <= c[1] + CLUSTER_SEC * 1000:
                    idx = i
                    break
            if idx is None:
                idx = len(closes) - 1
            b = buckets[idx]
            ty = x.get("incomeType", "")
            if ty == "REALIZED_PNL":
                b["pnl"] += x["_v"]; b["n"] += 1
            elif ty == "COMMISSION":
                b["fee"] += x["_v"]
            elif ty == "FUNDING_FEE":
                b["fund"] += x["_v"]
            else:
                b["other"] += x["_v"]
        for b in buckets:
            b["net"] = b["pnl"] + b["fee"] + b["fund"] + b["other"]
            trades.append(b)

    trades.sort(key=lambda z: z["t"])
    return trades


def last_trade_income(fs, since_ms):
    """
    PnL ТОЛЬКО последней закрытой сделки по монете.
    Раньше складывался весь income за сутки: если по HBAR было три входа,
    все три попадали в одно сообщение о закрытии (-$45 вместо -$15).
    """
    empty = {"pnl": 0.0, "fee": 0.0, "fund": 0.0, "other": 0.0, "n": 0}
    try:
        rows = binance_income(since_ms, fs)
    except Exception as e:
        print("income error:", e)
        return empty
    groups = group_trades(rows)
    if not groups:
        return empty
    g = groups[-1]          # самая свежая группа = только что закрытая сделка
    return {"pnl": g["pnl"], "fee": g["fee"], "fund": g["fund"],
            "other": g["other"], "n": g["n"]}


def fut_candidates(sym):
    return [sym + "USDT", "1000" + sym + "USDT"]


def save():
    try:
        with open(DB, "w") as f:
            json.dump({"tracked": {str(k): v for k, v in tracked.items()},
                       "users": list(users),
                       "opened_keys": list(opened_keys),
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
        opened_keys.clear()
        opened_keys.update(d.get("opened_keys", []))
        print("загружено: сделок", sum(len(v) for v in tracked.values()),
              "| история", sum(len(v) for v in history.values()))
    except Exception:
        print("нет сохранённых данных, старт с нуля")


def _api_get(path, tries=None):
    """GET по рыночным данным с перебором зеркал Binance"""
    n = len(DATA_HOSTS)
    order = [(_host_idx["i"] + k) % n for k in range(n)]
    last = None
    for k in order:
        host = DATA_HOSTS[k]
        p = path
        if "fapi.binance.com" in host:
            p = path.replace("/api/v3/", "/fapi/v1/")
        try:
            r = requests.get(host + p, timeout=15)
            if r.status_code == 200:
                _host_idx["i"] = k          # запоминаем рабочее зеркало
                return r.json()
            last = "HTTP " + str(r.status_code)
        except Exception as e:
            last = type(e).__name__
    raise Exception("все источники недоступны (" + str(last) + ")")


def get_klines_tf(symbol, interval, limit=250):
    """свечи любого таймфрейма (для пробоя нужны и 4ч, и 1ч)"""
    r = _api_get(f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not isinstance(r, list) or len(r) < 3:
        raise Exception("нет данных " + symbol)
    return [{"o": float(c[1]), "h": float(c[2]), "l": float(c[3]),
             "c": float(c[4]), "v": float(c[5]), "close_t": int(c[6])} for c in r]


def get_klines(symbol, limit=250):
    return get_klines_tf(symbol, "4h", limit)


def get_price(symbol):
    r = _api_get(f"/api/v3/ticker/price?symbol={symbol}")
    if not isinstance(r, dict) or "price" not in r:
        raise Exception("нет цены " + symbol)
    return float(r["price"])


def fmt(p):
    if p >= 100: return format(p, ".2f")
    if p >= 1: return format(p, ".4f")
    return format(p, ".6f")


def money(v):
    return ("+" if v >= 0 else "-") + "$" + format(abs(v), ".2f")


def fn(step):
    funnel[step] = funnel.get(step, 0) + 1


def top_coins():
    r = _api_get("/api/v3/ticker/24hr")
    usdt = [x for x in r if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    out = []
    # берём широкий пул: до 120 монет по обороту. Фьючерсный фильтр применим мягко —
    # если _filters пуст (не загрузился) или монеты нет, всё равно пробуем.
    for x in usdt[:120]:
        sym = x["symbol"].replace("USDT", "")
        if sym.upper() in BLACKLIST or x["symbol"].upper() in BLACKLIST:
            continue
        # если фильтры загружены — предпочитаем торгуемые фьючерсом, но не жёстко
        if _filters and (sym + "USDT") not in _filters:
            continue
        out.append(sym)
        if len(out) >= CAND_COINS:
            break
    # если после фьючерсного фильтра почти пусто — берём топ по обороту без него
    if len(out) < 10:
        out = []
        for x in usdt[:CAND_COINS + 10]:
            sym = x["symbol"].replace("USDT", "")
            if sym.upper() in BLACKLIST or x["symbol"].upper() in BLACKLIST:
                continue
            out.append(sym)
            if len(out) >= CAND_COINS:
                break
    return out


def fetch_funding_intervals():
    """
    Интервал фандинга по символам (часов): 8, 4 или даже 1.
    fundingInfo отдаёт только те символы, у кого интервал НЕ дефолтный (8ч).
    Остальные — 8ч по умолчанию.
    """
    intervals = {}
    try:
        r = requests.get(FAPI + "/fapi/v1/fundingInfo", timeout=15)
        arr = r.json()
        if isinstance(arr, list):
            for x in arr:
                sym = x.get("symbol", "")
                h = x.get("fundingIntervalHours")
                if sym and h:
                    intervals[sym] = int(h)
    except Exception as e:
        print("fundingInfo:", str(e)[:60])
    return intervals


def fetch_funding():
    """
    Текущий фандинг по всем USDT-фьючерсам.
    Возвращает {sym: (rate_pct, interval_hours)}.
    rate_pct — ставка за ОДИН интервал; interval_hours — 8, 4 или 1.
    Знак: >0 лонги платят шортам (шортим), <0 шорты платят лонгам (лонгуем).
    """
    out = {}
    intervals = fetch_funding_intervals()
    try:
        r = requests.get(FAPI + "/fapi/v1/premiumIndex", timeout=20)
        arr = r.json()
    except Exception as e:
        print("funding fetch:", e)
        return out
    if not isinstance(arr, list):
        return out
    for x in arr:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rate = float(x.get("lastFundingRate", 0)) * 100
        except Exception:
            continue
        hrs = intervals.get(sym, 8)          # по умолчанию 8ч
        out[sym] = (rate, hrs)
    return out


def daily_funding_pct(rate, hrs):
    """приводим ставку за интервал к суточной доходности, %"""
    if hrs <= 0:
        hrs = 8
    payouts_per_day = 24.0 / hrs
    return rate * payouts_per_day




# ======================= СТРАТЕГИЯ «ЛЕСЕНКА ПЕРЕД ФАНДИНГОМ» =======================
# Идея: при сильно ОТРИЦАТЕЛЬНОМ фандинге шорты давят цену вниз перед выплатой.
# За NN секунд до выплаты ставим лесенку ЛИМИТНЫХ ордеров на покупку ниже рынка.
# Цена проваливается -> набираем лонг по хорошим ценам -> после выплаты отскок ->
# закрываем в плюс. Шаг лесенки = половина размера фандинга, равными частями.
#
# ЧЕСТНЫЙ СЧЁТ: каждая сделка логируется с реальным результатом (отскок или стоп),
# чтобы прямо на боте видеть, работает идея или нет.

# --- параметры стратегии (крутятся через Railway) ---
LADDER_TRIGGER = float(os.environ.get("LADDER_TRIGGER", "0.8"))   # входим при |фандинг| >= этого %
LADDER_LEGS    = int(os.environ.get("LADDER_LEGS", "4"))          # сколько лимиток в лесенке
LADDER_SIZES   = os.environ.get("LADDER_SIZES", "100,200,300,400")# размеры лимиток $, через запятую
ENTER_BEFORE_S = int(os.environ.get("ENTER_BEFORE_S", "15"))      # за сколько секунд до выплаты ставить лимитки
TAKE_PCT       = float(os.environ.get("TAKE_PCT", "1.0"))         # отскок вверх на % от средней -> продаём
STOP_PCT       = float(os.environ.get("STOP_PCT", "3.0"))         # падение на % от средней -> стоп
WAIT_FILL_MIN  = int(os.environ.get("WAIT_FILL_MIN", "10"))       # ждём набор лимиток, минут
MAX_HOLD_MIN   = int(os.environ.get("MAX_HOLD_MIN", "120"))       # макс. удержание позиции, минут
LAD_SCAN_SEC   = int(os.environ.get("LAD_SCAN_SEC", "30"))        # период проверки времени выплаты, сек


def ladder_sizes():
    try:
        return [float(x) for x in LADDER_SIZES.split(",")][:LADDER_LEGS]
    except Exception:
        return [100, 200, 300, 400][:LADDER_LEGS]


def next_funding(symbol):
    """время следующей выплаты (ms) и текущая ставка (%) по монете"""
    try:
        r = _api_get_fapi("/fapi/v1/premiumIndex?symbol=" + symbol)
        if isinstance(r, dict):
            nt = int(r.get("nextFundingTime", 0))
            rate = float(r.get("lastFundingRate", 0)) * 100
            return nt, rate
    except Exception as e:
        print("next_funding", symbol, ":", str(e)[:60])
    return 0, 0.0


def _api_get_fapi(path):
    """GET к fapi (для premiumIndex — нужен именно фьючерсный домен)"""
    url = FAPI + path
    r = requests.get(url, timeout=15)
    return r.json()


def place_limit(fs, side, price, qty):
    """лимитный ордер"""
    p = {"symbol": fs, "side": side, "type": "LIMIT",
         "price": fmt_tick(fs, price), "quantity": qty,
         "timeInForce": "GTC"}
    return signed_post("/fapi/v1/order", p)


def fmt_tick(fs, price):
    f = _filters.get(fs)
    if f:
        return fmt(step_round(price, f["tick"]))
    return fmt(price)


def cancel_all(fs):
    try:
        signed_delete("/fapi/v1/allOpenOrders", {"symbol": fs})
    except Exception as e:
        print("cancel_all", fs, ":", str(e)[:50])


def find_ladder_candidates():
    """ищем монеты с сильным отрицательным фандингом и близкой выплатой"""
    funnel.clear()
    fund = fetch_funding()      # {sym: (rate, hrs)}
    funnel["монет с фандингом"] = len(fund)
    coins = top_coins()[:CAND_COINS]
    funnel["монет в пуле"] = len(coins)
    now_ms = int(time.time() * 1000)
    out = []
    for c in coins:
        fs = c + "USDT"
        v = fund.get(fs)
        if v is None:
            continue
        rate, hrs = v
        # стратегия для ОТРИЦАТЕЛЬНОГО фандинга (шорты давят -> лонгуем на просадке)
        if rate > -LADDER_TRIGGER:
            continue
        fn("жирный минус-фандинг")
        nt, live_rate = next_funding(fs)
        if nt <= 0:
            continue
        mins_left = (nt - now_ms) / 60000
        out.append({"sym": c, "fs": fs, "rate": rate, "next": nt,
                    "mins_left": mins_left})
    funnel["готовы к лесенке"] = len(out)
    out.sort(key=lambda x: x["mins_left"])   # ближайшие выплаты первыми
    return out


def open_ladder(chat_id, cand):
    """
    Ставим лесенку лимиток на покупку.
    Шаг = половина |фандинга|, равными частями между ценой и (цена - фандинг/2).
    """
    fs = cand["fs"]
    price = get_price(fs)
    depth = abs(cand["rate"]) / 2 / 100        # половина фандинга в долях
    sizes = ladder_sizes()
    n = len(sizes)
    f = _filters.get(fs)
    if not f:
        return False, "нет фильтров " + fs

    try:
        signed_post("/fapi/v1/leverage", {"symbol": fs, "leverage": LEVERAGE})
    except Exception:
        pass

    placed = []
    total_qty = 0.0
    weighted = 0.0
    for i in range(n):
        # равные части вниз: от цены до цены*(1-depth)
        frac = (i + 1) / n * depth
        lp = price * (1 - frac)
        qty = step_round(sizes[i] / lp, f["step"])
        if qty < f["minQty"] or qty * lp < f["minNot"]:
            continue
        try:
            place_limit(fs, "BUY", lp, qty)
            placed.append((lp, qty))
            total_qty += qty
            weighted += lp * qty
        except Exception as e:
            print("limit", fs, i, ":", str(e)[:60])
    if not placed:
        return False, "лимитки не встали"

    avg_planned = weighted / total_qty if total_qty else price
    tracked.setdefault(chat_id, [])
    tracked[chat_id].append({
        "kind": "ladder", "sym": cand["sym"], "fs": fs,
        "rate0": cand["rate"], "price0": price,
        "avg_planned": avg_planned, "planned_qty": total_qty,
        "legs": placed, "state": "waiting_fill",
        "t0": time.time(), "next": cand["next"],
        "bn_t": int(time.time() * 1000) - 60000,
    })
    save()
    lines = "\n".join("  $" + format(s, ".0f") + " @ " + fmt(p)
                      for (p, q), s in zip(placed, sizes))
    bot.send_message(chat_id,
        "🪜 ЛЕСЕНКА ВЫСТАВЛЕНА: " + cand["sym"] + "\n"
        "фандинг " + format(cand["rate"], "+.3f") + "% · выплата через "
        + format(cand["mins_left"], ".0f") + " мин\n"
        "лимитки на покупку:\n" + lines + "\n"
        "жду набора, потом отскок +" + format(TAKE_PCT, ".1f") + "% или стоп -"
        + format(STOP_PCT, ".1f") + "%", reply_markup=menu())
    return True, "ok"


def manage_ladder(chat_id, s):
    """
    Ведём позицию-лесенку:
      waiting_fill -> ждём набора лимиток (WAIT_FILL_MIN)
      in_position  -> ждём отскок (TAKE) или стоп (STOP) или таймаут
    Возвращает True если позицию надо убрать из трекинга.
    """
    fs = s["fs"]
    try:
        pos = binance_positions()
    except Exception:
        return False
    amt = abs(pos.get(fs, {}).get("amt", 0))
    entry = pos.get(fs, {}).get("entry", 0)
    held_min = (time.time() - s.get("t0", time.time())) / 60

    if s["state"] == "waiting_fill":
        if amt > 0:
            # часть лесенки набралась — переходим в позицию
            s["state"] = "in_position"
            s["bn_entry"] = entry
            cancel_all(fs)     # снимаем неисполненные лимитки
            try:
                bot.send_message(chat_id, "✅ " + s["sym"]
                    + ": лесенка набрала лонг по средней " + fmt(entry)
                    + "\nжду отскок +" + format(TAKE_PCT, ".1f") + "%")
            except Exception:
                pass
            return False
        # лимитки не набрались за WAIT_FILL_MIN — отменяем, сделки нет
        if held_min >= WAIT_FILL_MIN:
            cancel_all(fs)
            try:
                bot.send_message(chat_id, "⏭ " + s["sym"]
                    + ": цена не провалилась до лимиток за " + str(WAIT_FILL_MIN)
                    + " мин, отменил. Сделки нет.")
            except Exception:
                pass
            return True
        return False

    # in_position: ждём отскок или стоп
    if amt <= 0:
        # позиции нет — закрылась (стоп/тейк исполнился) — фиксируем результат
        record_ladder_close(chat_id, s, "закрыто")
        return True
    entry = s.get("bn_entry", entry)
    price = get_price(fs)
    move = (price - entry) / entry * 100 if entry else 0
    if move >= TAKE_PCT:
        close_now(fs, "BUY", amt)      # продаём лонг на отскоке
        record_ladder_close(chat_id, s, "🎯 отскок +" + format(move, ".1f") + "%")
        return True
    if move <= -STOP_PCT:
        close_now(fs, "BUY", amt)
        record_ladder_close(chat_id, s, "🛑 стоп " + format(move, ".1f") + "%")
        return True
    if held_min >= MAX_HOLD_MIN:
        close_now(fs, "BUY", amt)
        record_ladder_close(chat_id, s, "⏱ таймаут " + format(move, ".1f") + "%")
        return True
    return False


def record_ladder_close(chat_id, s, reason):
    """фиксируем результат сделки с РЕАЛЬНЫМ pnl с биржи"""
    fs = s["fs"]
    since = s.get("bn_t", int(time.time() * 1000) - 3600000)
    try:
        inc = last_trade_income(fs, since)
        net = inc["pnl"] + inc["fee"] + inc["fund"]
        gross, fee, fund = inc["pnl"], inc["fee"], inc["fund"]
    except Exception:
        net = gross = fee = fund = 0.0
    hist = history.setdefault(chat_id, [])
    hist.append({"sym": s["sym"], "side": "ladder", "result": reason,
                 "net": round(net, 2), "gross": round(gross, 2),
                 "fee": round(fee, 2), "fund": round(fund, 2),
                 "src": "binance", "t": int(time.time())})
    save()
    try:
        bot.send_message(chat_id,
            "🪜 ЛЕСЕНКА ЗАКРЫТА: " + s["sym"] + "\n" + reason + "\n"
            "💵 чистыми: " + money(net) + "  (тело " + money(gross)
            + " · фандинг " + money(fund) + " · комиссии " + money(fee) + ")",
            reply_markup=menu())
    except Exception:
        pass
    return net


# ---------- МЕНЮ ----------

auto_on = os.environ.get("AUTO", "1") == "1"


def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🪜 Найти лесенки")
    kb.row("📋 Мои сделки", "📊 Статистика")
    kb.row("🤖 Автоторговля", "🗑 Очистить")
    return kb


@bot.message_handler(commands=['start'])
def cmd_start(m):
    users.add(m.chat.id); save()
    k = "✅ Binance подключён" if has_keys() else "⚠️ нет ключей Binance"
    bot.send_message(m.chat.id,
        "🪜 Стратегия «Лесенка перед фандингом»\n"
        "При фандинге ≤ -" + str(LADDER_TRIGGER) + "% ставлю лесенку лимиток на покупку "
        "за " + str(ENTER_BEFORE_S) + " сек до выплаты.\n"
        "Цена проваливается → набираю лонг → отскок +" + str(TAKE_PCT) + "% → продаю.\n"
        "Стоп -" + str(STOP_PCT) + "%. Шаг лесенки = половина фандинга.\n\n" + k,
        reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🪜 Найти лесенки")
def btn_find(m):
    bot.send_message(m.chat.id, "Ищу монеты с жирным отрицательным фандингом...")
    try:
        cands = find_ladder_candidates()
        if not cands:
            bot.send_message(m.chat.id, "Сейчас нет монет с фандингом ≤ -"
                + str(LADDER_TRIGGER) + "%." + funnel_text(), reply_markup=menu())
            return
        head = "Нашёл " + str(len(cands)) + " кандидатов" + funnel_text()
        bot.send_message(m.chat.id, head)
        for c in cands[:6]:
            bot.send_message(m.chat.id,
                "💰 " + c["sym"] + "\n"
                "фандинг " + format(c["rate"], "+.3f") + "%\n"
                "выплата через " + format(c["mins_left"], ".0f") + " мин",
                reply_markup=menu())
            time.sleep(0.4)
    except Exception as e:
        bot.send_message(m.chat.id, "Ошибка: " + str(e)[:80], reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "📋 Мои сделки")
def btn_list(m):
    t = [x for x in tracked.get(m.chat.id, []) if x.get("kind") == "ladder"]
    if not t:
        bot.send_message(m.chat.id, "Нет активных лесенок.", reply_markup=menu())
        return
    for s in t:
        held = (time.time() - s.get("t0", time.time())) / 60
        st = {"waiting_fill": "⏳ жду набора лимиток",
              "in_position": "📈 в позиции, жду отскок"}.get(s.get("state"), s.get("state"))
        bot.send_message(m.chat.id,
            "🪜 " + s["sym"] + "  " + st + "\n"
            "фандинг входа " + format(s.get("rate0", 0), "+.3f") + "%\n"
            "средняя (план) " + fmt(s.get("avg_planned", 0)) + "\n"
            "в работе " + format(held, ".0f") + " мин", reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def btn_stats(m):
    bot.send_message(m.chat.id, "Считаю статистику...")
    hist = history.get(m.chat.id, [])
    lad = [h for h in hist if h.get("side") == "ladder"]
    if not lad:
        bot.send_message(m.chat.id, "Пока нет закрытых сделок.", reply_markup=menu())
        return
    total = sum(h["net"] for h in lad)
    wins = [h for h in lad if h["net"] > 0]
    losses = [h for h in lad if h["net"] < 0]
    gross = sum(h.get("gross", 0) for h in lad)
    fee = sum(h.get("fee", 0) for h in lad)
    fund = sum(h.get("fund", 0) for h in lad)
    wr = len(wins) / len(lad) * 100 if lad else 0
    L = ["📊 ЛЕСЕНКА · с " + STATS_START + " МСК",
         str(len(lad)) + " закрытых сделок", "",
         "💵 Чистыми: " + money(total), "",
         "🟢 В плюс: " + str(len(wins)) + "  (" + format(wr, ".0f") + "%)",
         "🔴 В минус: " + str(len(losses)), "",
         "📈 Тело: " + money(gross),
         "💸 Комиссии: " + money(fee),
         "💱 Фандинг: " + money(fund)]
    bot.send_message(m.chat.id, "\n".join(L), reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🤖 Автоторговля")
def btn_auto(m):
    global auto_on
    auto_on = not auto_on
    bot.send_message(m.chat.id, "🤖 Автоторговля: " + ("ВКЛ ✅" if auto_on else "ВЫКЛ ⛔"),
                     reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🗑 Очистить")
def btn_clear(m):
    tracked[m.chat.id] = []
    history[m.chat.id] = []
    save()
    bot.send_message(m.chat.id, "Очищено.", reply_markup=menu())


@bot.message_handler(func=lambda m: True)
def catch_all(m):
    users.add(m.chat.id); save()
    bot.send_message(m.chat.id, "Меню ниже 👇", reply_markup=menu())


# ---------- ГЛАВНЫЙ ЦИКЛ ----------

def ladder_loop():
    last_scan = 0
    while True:
        try:
            # 1) ведём открытые лесенки
            for chat_id, lst in list(tracked.items()):
                for s in list(lst):
                    if s.get("kind") != "ladder":
                        continue
                    try:
                        if manage_ladder(chat_id, s):
                            lst.remove(s); save()
                    except Exception as e:
                        print("manage", s.get("sym"), ":", str(e)[:60])

            # 2) ищем новые входы: монеты, у которых выплата через ~ENTER_BEFORE_S сек
            if auto_on and has_keys() and time.time() - last_scan >= LAD_SCAN_SEC:
                last_scan = time.time()
                try:
                    cands = find_ladder_candidates()
                    busy = set()
                    for chat_id, lst in tracked.items():
                        for s in lst:
                            if s.get("kind") == "ladder":
                                busy.add(s["sym"])
                    for c in cands:
                        if c["sym"] in busy:
                            continue
                        secs_left = c["mins_left"] * 60
                        # ставим лесенку, когда до выплаты осталось <= ENTER_BEFORE_S
                        if 0 < secs_left <= ENTER_BEFORE_S:
                            for uid in list(users):
                                try:
                                    open_ladder(uid, c)
                                except Exception as e:
                                    print("open", c["sym"], ":", str(e)[:60])
                except Exception as e:
                    print("scan loop:", str(e)[:60])
        except Exception as e:
            print("loop error:", str(e)[:60])
        time.sleep(3)


def funnel_text():
    if not funnel:
        return ""
    order = ["монет с фандингом", "монет в пуле", "жирный минус-фандинг", "готовы к лесенке"]
    t = "\n📉 ВОРОНКА:"
    for k in order:
        if funnel.get(k):
            t += "\n  " + k + ": " + str(funnel[k])
    return t


# ---------- ЗАПУСК ----------

load()
if CHAT_ID:
    try:
        users.add(int(CHAT_ID)); save()
    except Exception:
        pass
print("получателей:", len(users), "| стратегия: ЛЕСЕНКА")

if has_keys():
    sync_time()
    load_filters()
    try:
        pp = binance_positions()
        print("Binance OK, открытых ног:", len(pp))
    except Exception as e:
        print("Binance ключи не работают:", str(e)[:60])

print("параметры: триггер -" + str(LADDER_TRIGGER) + "% | лимиток "
      + str(LADDER_LEGS) + " | тейк +" + str(TAKE_PCT) + "% | стоп -" + str(STOP_PCT) + "%")

threading.Thread(target=ladder_loop, daemon=True).start()

print("бот запущен")
while True:
    try:
        bot.polling(none_stop=True, timeout=30)
    except Exception as e:
        print("polling error:", str(e)[:60])
        time.sleep(5)
