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

SIZE = 1000                                             # номинал КАЖДОЙ ноги
# --- ФАНДИНГ-ФАРМ с хеджем ---
MIN_FUNDING = float(os.environ.get("MIN_FUNDING", "0.5"))   # мин. |фандинг| за 8ч, % для входа
EXIT_FUNDING = float(os.environ.get("EXIT_FUNDING", "0.2")) # выход когда |фандинг| упал ниже, %
LEG_STOP = float(os.environ.get("LEG_STOP", "4.0"))         # стоп: расхождение ног в % (хедж не держит)
MIN_CORR = float(os.environ.get("MIN_CORR", "0.8"))         # корреляция монеты и хеджа
CORR_WIN = int(os.environ.get("CORR_WIN", "200"))           # окно корреляции, свечей
TF = os.environ.get("PAIR_TF", "1h")                        # ТФ для расчёта корреляции хеджа
CAND_COINS = int(os.environ.get("CAND_COINS", "60"))        # из скольких монет ищем хедж
MAX_HOLD_H = float(os.environ.get("MAX_HOLD_H", "168"))     # макс. удержание, часов (неделя)
MAX_RISK = float(os.environ.get("MAX_RISK", "3.0"))
FEE = 0.055
BE_BAND = 5.0
BTC_FLAT = 0.7
BTC_FILTER = False
SCAN_EVERY = int(os.environ.get("SCAN_EVERY", "600"))       # период автоскана, сек (10 мин)
# заглушки старых z-параметров, чтобы не падали ссылки в переиспользуемом коде
Z_ENTRY = Z_EXIT = Z_STOP = 0.0
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
    kb.row("🔍 Найти пробои")
    kb.row("📋 Мои сделки", "📊 Статистика")
    kb.row("📐 Анализ", "🤖 Автоторговля")
    kb.row("🗑 Очистить")
    return kb


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



# ======================= ФАНДИНГ-ФАРМ =======================
# Классика: две коррелированные монеты, спред, z-score.
# |z|>=Z_ENTRY -> шортим сильную, лонгуем слабую (ставка на схождение).
# |z|<=Z_EXIT -> закрываем обе ноги (сошлись, профит).
# |z|>=Z_STOP -> закрываем обе ноги (разъехались, убыток).
# Держим не дольше MAX_HOLD_H часов.

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


def fetch_funding():
    """
    Текущий фандинг по всем USDT-фьючерсам.
    Возвращает {sym: rate_pct}, где rate_pct — ставка за 8ч в процентах.
    Знак: >0 лонги платят шортам (шортим), <0 шорты платят лонгам (лонгуем).
    """
    out = {}
    try:
        # premiumIndex отдаёт lastFundingRate по каждому символу
        d = signed_get("/fapi/v1/premiumIndex") if False else None
    except Exception:
        d = None
    # публичный эндпоинт, ключи не нужны — тянем через зеркало fapi
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
            rate = float(x.get("lastFundingRate", 0)) * 100   # в проценты
        except Exception:
            continue
        out[sym] = rate
    return out


def closes_tf(sym, limit):
    """закрытия свечей TF по монете; None при ошибке"""
    try:
        kl = get_klines_tf(sym + "USDT", TF, limit)
    except Exception:
        return None
    # достаточно данных для расчёта z-score (окно ZWIN + запас), а не половины запроса
    if not kl or len(kl) < ZWIN + 5:
        return None
    now_ms = int(time.time() * 1000)
    if kl[-1]["close_t"] > now_ms:      # выкидываем незакрытую свечу
        kl = kl[:-1]
    return [k["c"] for k in kl]


def zscore(spread):
    """z последнего значения спреда по окну ZWIN"""
    a = np.array(spread[-ZWIN:], dtype=float)
    if len(a) < ZWIN:
        return None, None, None
    m = a.mean()
    sd = a.std()
    if sd <= 0:
        return None, None, None
    z = (a[-1] - m) / sd
    return float(z), float(m), float(sd)


def pair_spread(ca, cb):
    """
    Лог-спред двух рядов цен: ln(A) - ln(B).
    Логарифм чтобы масштаб монет не искажал спред (BTC $60000 vs DOGE $0.1).
    """
    n = min(len(ca), len(cb))
    if n < ZWIN + 5:
        return None
    a = np.log(np.array(ca[-n:], dtype=float))
    b = np.log(np.array(cb[-n:], dtype=float))
    return (a - b).tolist()


def correlation(ca, cb):
    n = min(len(ca), len(cb), CORR_WIN)
    if n < 50:
        return 0.0
    a = np.array(ca[-n:], dtype=float)
    b = np.array(cb[-n:], dtype=float)
    # корреляция дневных изменений, а не самих цен (чтобы тренд не завышал corr)
    da = np.diff(a) / a[:-1]
    db = np.diff(b) / b[:-1]
    if da.std() == 0 or db.std() == 0:
        return 0.0
    return float(np.corrcoef(da, db)[0, 1])


def is_cointegrated(ca, cb):
    """
    Проверка коинтеграции пары (упрощённый Энгл-Грейнджер).
    Корреляция ловит совместное движение с рынком (ложные пары),
    коинтеграция — что СПРЕД реально возвращается к среднему.

    Логика: строим лог-спред, проверяем его на стационарность
    через тест "возврата к среднему" (regression ΔS на S[-1]):
    если коэффициент β в ΔS = α + β·S[-1] значимо отрицателен —
    спред тянет обратно к среднему, пара коинтегрирована.
    Возвращаем (ok, half_life) — период полураспада отклонения в свечах.
    """
    n = min(len(ca), len(cb), CORR_WIN)
    if n < 60:
        return False, None
    a = np.log(np.array(ca[-n:], dtype=float))
    b = np.log(np.array(cb[-n:], dtype=float))
    # хедж-коэффициент через МНК: a = α + β·b, спред = a - β·b
    B = np.vstack([b, np.ones(len(b))]).T
    try:
        beta, alpha = np.linalg.lstsq(B, a, rcond=None)[0]
    except Exception:
        return False, None
    spread = a - (beta * b + alpha)
    # тест возврата к среднему: ΔS[t] = c + k·S[t-1]
    s_lag = spread[:-1]
    ds = np.diff(spread)
    if s_lag.std() == 0:
        return False, None
    X = np.vstack([s_lag, np.ones(len(s_lag))]).T
    try:
        coef, res, *_ = np.linalg.lstsq(X, ds, rcond=None)
        k, c = coef[0], coef[1]
    except Exception:
        return False, None
    if k >= -1e-4:
        return False, None
    # t-статистика коэффициента k: насколько уверенно возврат к среднему,
    # а не случайность. Это и есть суть теста Дики-Фуллера.
    resid = ds - (k * s_lag + c)
    dof = len(s_lag) - 2
    if dof <= 0:
        return False, None
    sigma2 = float(resid @ resid) / dof
    sxx = float(((s_lag - s_lag.mean()) ** 2).sum())
    if sxx <= 0 or sigma2 <= 0:
        return False, None
    se_k = (sigma2 / sxx) ** 0.5
    t_stat = k / se_k                      # чем отрицательнее, тем сильнее коинтеграция
    # критическое значение ADF (грубо -2.9 при 5%); берём строже -3.0
    T_CRIT = float(os.environ.get("COINT_T", "-3.0"))
    if t_stat > T_CRIT:
        return False, None
    half_life = -np.log(2) / k
    HL_MAX = float(os.environ.get("HALFLIFE_MAX", "120"))   # свечей; дольше — не берём
    if half_life <= 0 or half_life > HL_MAX:
        return False, half_life
    return True, float(half_life)


def scan_pairs():
    """
    Фандинг-фарм. Ищем монету с |фандинг|>=MIN_FUNDING, к ней подбираем
    коррелированный хедж (corr>=MIN_CORR) с фандингом послабее.
    Направление: фандинг>0 -> ловим шортом монеты; фандинг<0 -> лонгом.
    Хедж — противоположной ногой, чтобы гасить движение тела.
    Возвращаем список готовых сетапов, сорт по |фандинг| убыв.
    """
    funnel.clear()
    fund = fetch_funding()
    funnel["монет с фандингом"] = len(fund)
    coins = top_coins()[:CAND_COINS]
    funnel["монет в пуле"] = len(coins)

    # кандидаты по фандингу: |ставка|>=порог
    hot = []
    for c in coins:
        fs = c + "USDT"
        rate = fund.get(fs)
        if rate is None:
            continue
        if abs(rate) >= MIN_FUNDING:
            hot.append((c, rate))
    hot.sort(key=lambda x: -abs(x[1]))
    funnel["жирный фандинг"] = len(hot)
    if not hot:
        return []

    # тянем свечи для корреляции — только по монетам, что реально нужны
    need = CORR_WIN + 5
    data = {}
    for c in coins:
        if c.upper() in BLACKLIST:
            continue
        cl = closes_tf(c, need)
        if cl and len(cl) >= 60:
            data[c] = cl
        time.sleep(0.05)

    found = []
    for main, rate in hot:
        if main not in data:
            continue
        side_main = "short" if rate > 0 else "long"     # как ловим фандинг
        # ищем лучший хедж: макс corr, фандинг не мешающий
        best = None
        for h in data:
            if h == main:
                continue
            hrate = fund.get(h + "USDT", 0)
            # хедж не должен сам жрать фандинг в ту же сторону сильно
            corr = correlation(data[main], data[h])
            if corr < MIN_CORR:
                continue
            fn("нашёлся хедж")
            # чистый фандинг = фандинг основной минус фандинг хеджа по модулю вклада
            # хедж встаёт противоположной ногой, его фандинг может помогать или мешать
            net = abs(rate) - max(0, (hrate if side_main == "short" else -hrate))
            if best is None or corr > best["corr"]:
                best = {"hedge": h, "corr": corr, "hrate": hrate, "net": net}
        if not best:
            fn("нет хеджа")
            continue
        if best["net"] < EXIT_FUNDING:
            fn("нетто мал")
            continue
        found.append({
            "main": main, "rate": rate, "side_main": side_main,
            "hedge": best["hedge"], "hrate": best["hrate"],
            "corr": best["corr"], "net": best["net"],
        })
    funnel["готовы к входу"] = len(found)
    found.sort(key=lambda x: -x["net"])
    return found


def grade_pair(p):
    """оценка сетапа 0..6: чем жирнее фандинг и выше corr хеджа, тем лучше"""
    pts = 0
    af = abs(p["rate"])
    if af >= 1.0: pts += 2
    elif af >= 0.5: pts += 1
    if p["corr"] >= 0.9: pts += 2
    elif p["corr"] >= 0.85: pts += 1
    if p["net"] >= 0.7: pts += 1
    if p["corr"] >= 0.95: pts += 1
    if pts >= 5: return pts, "🟢 ЖИРНЫЙ ФАНДИНГ"
    if pts >= 3: return pts, "🟡 СРЕДНИЙ"
    return pts, "🔴 СЛАБЫЙ"


def pair_card(p):
    pts, lab = grade_pair(p)
    if p["side_main"] == "short":
        main_leg = "🔴 ШОРТ " + p["main"] + "  (фандинг " + format(p["rate"], "+.3f") + "% платят нам)"
        hedge_leg = "🟢 ЛОНГ " + p["hedge"] + "  (хедж, corr " + format(p["corr"], ".2f") + ")"
    else:
        main_leg = "🟢 ЛОНГ " + p["main"] + "  (фандинг " + format(p["rate"], "+.3f") + "% платят нам)"
        hedge_leg = "🔴 ШОРТ " + p["hedge"] + "  (хедж, corr " + format(p["corr"], ".2f") + ")"
    day = p["net"] * 3
    return (
        "💰 " + p["main"] + " + хедж " + p["hedge"] + "\n"
        + lab + "  (" + str(pts) + "/6)\n\n"
        "⚡ фандинг: " + format(p["rate"], "+.3f") + "% / 8ч\n"
        "🧮 нетто после хеджа: ~" + format(p["net"], ".3f") + "% (≈" + format(day, ".2f") + "%/день)\n\n"
        + main_leg + "\n"
        + hedge_leg + "\n\n"
        "Собираем фандинг каждые 8ч, хедж гасит движение цены.\n"
        "Выход: фандинг < " + str(EXIT_FUNDING) + "% или ноги разъехались > " + str(LEG_STOP) + "%\n"
        "⚙️ по $" + str(SIZE) + " на ногу · плечо " + str(LEVERAGE) + "x"
    )


# ---------- ИСПОЛНЕНИЕ ПАРЫ (две ноги рынком) ----------

def _market_leg(fs, side, qty):
    return signed_post("/fapi/v1/order", {"symbol": fs, "side": side,
                                          "type": "MARKET", "quantity": qty})


def open_pair(chat_id, p):
    """
    Фандинг-фарм: основная нога ловит фандинг, хедж-нога гасит движение цены.
    side_main="short" -> шорт main + лонг hedge; "long" -> лонг main + шорт hedge.
    Если вторая нога не встала — первую немедленно закрываем.
    """
    main, hedge = p["main"], p["hedge"]
    fa = auto_symbol(main)      # основная нога (ловит фандинг)
    fb = auto_symbol(hedge)     # хедж
    if not fa or not fb:
        return False, "нет фьючерса на одну из монет"
    key = fa + "_" + fb
    if key in opened_keys:
        return False, "уже открывали"

    # стороны: main по side_main, hedge противоположно
    if p["side_main"] == "short":
        side_a, opp_a, side_b, opp_b = "SELL", "BUY", "BUY", "SELL"
    else:
        side_a, opp_a, side_b, opp_b = "BUY", "SELL", "SELL", "BUY"

    for fs in (fa, fb):
        try:
            signed_post("/fapi/v1/leverage", {"symbol": fs, "leverage": LEVERAGE})
        except Exception as e:
            print("leverage", fs, e)

    pa = get_price(fa); pb = get_price(fb)
    ffa = _filters[fa]; ffb = _filters[fb]
    qa = step_round(SIZE / pa, ffa["step"])
    qb = step_round(SIZE / pb, ffb["step"])
    if qa < ffa["minQty"] or qa * pa < ffa["minNot"]:
        return False, main + ": размер меньше минимума"
    if qb < ffb["minQty"] or qb * pb < ffb["minNot"]:
        return False, hedge + ": размер меньше минимума"

    # нога 1 — основная
    try:
        _market_leg(fa, side_a, qa)
    except Exception as e:
        return False, "основная нога (" + main + ") не встала: " + str(e)

    # нога 2 — хедж; если не встала, откатываем ногу 1
    try:
        _market_leg(fb, side_b, qb)
    except Exception as e:
        try:
            close_now(fa, side_a, qa)
            return False, ("хедж (" + hedge + ") не встал, основную закрыл: " + str(e))
        except Exception as e2:
            bot.send_message(chat_id, "🆘 " + main + " ОСТАЛАСЬ ОДНОЙ НОГОЙ, закрыть не смог: "
                             + str(e2) + "\nЗАКРОЙ РУКАМИ.")
            return False, "одна нога, закрыть не удалось"

    opened_keys.add(key)

    # аварийные стопы: широкий стоп на каждую ногу против её направления
    emerg = float(os.environ.get("EMERG_STOP", "8.0")) / 100
    sl_msgs = []
    try:
        # основная: если SELL -> стоп сверху, если BUY -> снизу
        sla = step_round(pa * (1 + emerg) if side_a == "SELL" else pa * (1 - emerg), ffa["tick"])
        place_cond(fa, opp_a, "STOP_MARKET", sla)
    except Exception as e:
        sl_msgs.append(main + " SL: " + str(e)[:50])
    try:
        slb = step_round(pb * (1 + emerg) if side_b == "SELL" else pb * (1 - emerg), ffb["tick"])
        place_cond(fb, opp_b, "STOP_MARKET", slb)
    except Exception as e:
        sl_msgs.append(hedge + " SL: " + str(e)[:50])
    if sl_msgs:
        try:
            bot.send_message(chat_id, "⚠️ сетап открыт, но аварийный стоп не встал:\n"
                             + "\n".join(sl_msgs))
        except Exception:
            pass

    return True, {"fa": fa, "fb": fb, "qa": qa, "qb": qb, "pa": pa, "pb": pb,
                  "side_a": side_a, "side_b": side_b}


def close_pair(s, reason):
    """
    Закрываем обе ноги. Снимаем аварийные стопы, затем закрываем по РЕАЛЬНОМУ
    размеру с биржи (не по сохранённому qty). Если ноги уже нет на бирже
    (amt==0) — считаем её закрытой, а не шлём reduceOnly на пустую позицию.
    """
    for fs in (s["fa"], s["fb"]):
        try:
            cancel_algo(fs)
        except Exception as e:
            print("cancel emerg", fs, ":", str(e)[:60])
    try:
        pos = binance_positions()
    except Exception:
        pos = {}
    ok = True
    for fs, side in ((s["fa"], s.get("bn_side_a", "BUY")),
                     (s["fb"], s.get("bn_side_b", "SELL"))):
        amt = abs(pos.get(fs, {}).get("amt", 0))
        if amt <= 0:
            continue                      # ноги уже нет — успех, ничего не шлём
        try:
            close_now(fs, side, amt)      # close_now сам шлёт противоположный
        except Exception as e:
            msg = str(e)
            if "-2022" in msg or "ReduceOnly" in msg or "-4046" in msg:
                continue
            print("close leg", fs, ":", msg[:80]); ok = False
    opened_keys.discard(s["fa"] + "_" + s["fb"])
    return ok


# ---------- МЕНЮ ----------

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти фандинг")
    kb.row("📋 Мои фармы", "📊 Статистика")
    kb.row("🤖 Автоторговля")
    kb.row("🗑 Очистить")
    return kb


# ---------- ХЕНДЛЕРЫ ----------

@bot.message_handler(commands=['start'])
def start(m):
    users.add(m.chat.id)
    save()
    k = ("✅ Binance подключён — статистика с " + STATS_START + " МСК.") if has_keys() \
        else "⚠️ Binance не подключён — добавь ключи в Railway."
    bot.send_message(m.chat.id,
        "Фандинг-фарм 💰\n"
        "Ищу монету с |фандинг|≥" + str(MIN_FUNDING) + "% за 8ч, беру её под фандинг + хедж.\n"
        "Собираю фандинг каждые 8ч, хедж гасит движение цены.\n"
        "Выход: фандинг < " + str(EXIT_FUNDING) + "% или ноги разъехались > " + str(LEG_STOP) + "%.\n\n" + k, reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🔍 Найти фандинг")
def btn_scan(m):
    users.add(m.chat.id)
    save()
    bot.send_message(m.chat.id, "Сканирую фандинг по топ-" + str(CAND_COINS) + " монетам... жди 1-3 мин")
    do_scan(m.chat.id, manual=True)


@bot.message_handler(func=lambda m: m.text == "📋 Мои фармы")
def btn_list(m):
    t = [x for x in tracked.get(m.chat.id, []) if x.get("kind") == "pair"]
    if not t:
        bot.send_message(m.chat.id, "Нет открытых пар.", reply_markup=menu())
        return
    for s in list(t):
        try:
            rate = s.get("rate_last")
            if rate is None:
                rate = live_funding(s)
            div = s.get("div_last")
            if div is None:
                div = legs_divergence(s)
            held_h = (time.time() - s.get("t0_s", time.time())) / 3600

            if rate is not None and abs(rate) < EXIT_FUNDING + 0.05:
                mark = "🟢 фандинг гаснет — скоро выход"
            elif div >= LEG_STOP - 0.5:
                mark = "🔴 хедж на пределе"
            else:
                mark = "🟡 собираем фандинг"

            if s["side_main"] == "short":
                legs = "🔴 ШОРТ " + s["main"] + "   🟢 ЛОНГ " + s["hedge"]
            else:
                legs = "🟢 ЛОНГ " + s["main"] + "   🔴 ШОРТ " + s["hedge"]

            rate_t = format(rate, "+.3f") + "%" if rate is not None else "?"
            day = (abs(rate) * 3) if rate is not None else 0
            txt = (
                "💰 " + s["main"] + " + " + s["hedge"] + "   " + mark + "\n\n"
                + legs + "\n\n"
                "⚡ фандинг сейчас: " + rate_t + "  (≈" + format(day, ".2f") + "%/день)\n"
                "🧮 расхождение ног: " + format(div, ".2f") + "%  (стоп при " + str(LEG_STOP) + "%)\n"
                "вошли при " + format(s.get("rate0", 0), "+.3f") + "% · corr " + format(s.get("corr", 0), ".2f") + "\n"
                "⏱ в позиции " + format(held_h, ".1f") + " ч из " + str(int(MAX_HOLD_H))
            )
            bot.send_message(m.chat.id, txt, reply_markup=menu())
        except Exception as e:
            bot.send_message(m.chat.id, s["main"] + "/" + s["hedge"] + ": " + str(e)[:60])
        time.sleep(0.3)


@bot.message_handler(func=lambda m: m.text == "📊 Статистика")
def btn_stats(m):
    if not has_keys():
        bot.send_message(m.chat.id, "⚠️ Binance не подключён.", reply_markup=menu())
        return
    bot.send_message(m.chat.id, "Тяну реальные сделки с Binance...")
    try:
        rows = income_all(stats_start_ms())
        rows = [x for x in rows if (x.get("symbol") or "").upper() not in STATS_IGNORE]
        if not rows:
            bot.send_message(m.chat.id, "📊 С " + STATS_START + " МСК сделок нет.", reply_markup=menu())
            return
        tot = split_income(rows)
        real = tot["pnl"] + tot["fee"] + tot["fund"] + tot["other"]
        # для пар считаем закрытия по кластерам (нога A и нога B закрываются вместе)
        tr = group_trades(rows)
        # пара = два закрытия рядом по времени; но для простоты статистики берём разбивку по монетам
        vals = [x["net"] for x in tr]
        total = len(vals)
        wins = [x for x in vals if x > BE_BAND]
        losses = [x for x in vals if x < -BE_BAND]
        bes = [x for x in vals if abs(x) <= BE_BAND]
        wr = (len(wins) / total * 100) if total else 0
        avg_w = float(np.mean(wins)) if wins else 0.0
        avg_l = float(np.mean([abs(x) for x in losses])) if losses else 0.0
        try:
            pos = {k: v for k, v in binance_positions().items() if k.upper() not in STATS_IGNORE}
        except Exception:
            pos = {}
        upnl = sum(v["upnl"] for v in pos.values())
        L = []
        L.append("📊 ФАНДИНГ-ФАРМ · с " + STATS_START + " МСК")
        L.append(str(total) + " закрытий ног · " + str(len(pos)) + " открытых ног")
        L.append("")
        L.append("💵 Чистыми:  " + money(real))
        if pos:
            L.append("📌 Плавает:  " + money(upnl))
            L.append("━━━━━━━━━━━━")
            L.append("ИТОГО:       " + money(real + upnl))
        L.append("")
        L.append("🟢 В плюс:   " + str(len(wins)) + "   (" + format(wr, ".0f") + "%)")
        L.append("🔴 В минус:  " + str(len(losses)))
        L.append("🛡 В ноль:   " + str(len(bes)))
        L.append("")
        L.append("📈 Грязный: " + money(tot["pnl"]))
        L.append("💸 Комиссии: " + money(tot["fee"]))
        L.append("💱 Фандинг: " + money(tot["fund"]))
        L.append("")
        L.append("⚠️ ноги считаются раздельно; пара = две строки")
        bot.send_message(m.chat.id, "\n".join(L), reply_markup=menu())
    except Exception as e:
        bot.send_message(m.chat.id, "Ошибка Binance: " + str(e), reply_markup=menu())


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
        bot.send_message(m.chat.id,
            "🤖 АВТОТОРГОВЛЯ ВКЛЮЧЕНА\n\n"
            "Вхожу в пары с оценкой ≥" + str(AUTO_MIN_PTS) + "/6\n"
            "По $" + str(SIZE) + " на ногу · плечо " + str(LEVERAGE) + "x\n"
            "Потолок: " + str(MAX_POS) + " ног (сейчас " + str(pos) + ")\n"
            "Выход по z автоматом. Руками не трогай.", reply_markup=menu())
    else:
        bot.send_message(m.chat.id, "🤖 Автоторговля ВЫКЛЮЧЕНА.\nОткрытые пары продолжаю вести до выхода по z.", reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🗑 Очистить")
def btn_clear(m):
    tracked[m.chat.id] = []
    save()
    bot.send_message(m.chat.id, "Отслеживание снято. Статистика сохранена.\n(позиции на бирже не тронуты)", reply_markup=menu())


def funnel_text():
    if not funnel:
        return ""
    order = ["монет с фандингом", "монет в пуле", "жирный фандинг",
             "нашёлся хедж", "нет хеджа", "нетто мал", "готовы к входу"]
    t = "\n📉 ВОРОНКА:"
    for k in order:
        if funnel.get(k):
            t += "\n  " + k + ": " + str(funnel[k])
    return t


# ---------- ТЕКУЩИЙ z ПО ОТКРЫТОЙ ПАРЕ ----------

def live_funding(s):
    """текущий фандинг основной ноги, %"""
    fund = fetch_funding()
    return fund.get(s["main"] + "USDT")


def legs_divergence(s):
    """насколько ноги разъехались с момента входа, % (хедж не держит если велико)"""
    try:
        pa = get_price(s["fa"]); pb = get_price(s["fb"])
    except Exception:
        return 0.0
    p0a = s.get("bn_pa", pa); p0b = s.get("bn_pb", pb)
    if p0a <= 0 or p0b <= 0:
        return 0.0
    # движение основной и хеджа с входа
    ma = (pa - p0a) / p0a * 100
    mb = (pb - p0b) / p0b * 100
    # при исправном хедже ноги идут в одну сторону и разница мала
    return abs(ma - mb)


# ---------- СКАН + АВТОВХОД ----------

def do_scan(chat_id, manual=False):
    try:
        setups = scan_pairs()
        busy = set()
        for x in tracked.get(chat_id, []):
            if x.get("kind") == "pair":
                busy.add(x["main"]); busy.add(x["hedge"])
        fresh = [p for p in setups if p["main"] not in busy and p["hedge"] not in busy]

        if auto_on and has_keys():
            auto_enter(chat_id, fresh)

        if not setups:
            if manual:
                bot.send_message(chat_id, "Жирного фандинга сейчас нет.\n"
                                 "(нужен |фандинг|≥" + str(MIN_FUNDING) + "% и хедж corr≥" + str(MIN_CORR) + ")"
                                 + funnel_text(), reply_markup=menu())
            return
        show = fresh[:6]
        if not show:
            if manual:
                bot.send_message(chat_id, "Сетапы есть, но их монеты уже в работе."
                                 + funnel_text(), reply_markup=menu())
            return
        head = "Нашёл сетапов: " + str(len(fresh))
        if manual:
            head += funnel_text()
        bot.send_message(chat_id, head)
        for p in show:
            bot.send_message(chat_id, pair_card(p), reply_markup=menu())
            time.sleep(0.6)
    except Exception as e:
        print("scan error:", e)
        if manual:
            bot.send_message(chat_id, "Ошибка при поиске.", reply_markup=menu())


def auto_enter(chat_id, cands):
    try:
        pos = binance_positions()
    except Exception as e:
        print("auto positions:", e)
        return
    free = MAX_POS - len(pos)          # каждая пара занимает 2 ноги
    if free < 2:
        return
    for p in cands:
        if free < 2:
            break
        if grade_pair(p)[0] < AUTO_MIN_PTS:
            continue
        fa = auto_symbol(p["main"]); fb = auto_symbol(p["hedge"])
        if not fa or not fb or fa in pos or fb in pos:
            continue
        try:
            ok, info = open_pair(chat_id, p)
        except Exception as e:
            bot.send_message(chat_id, "🤖 ❌ " + p["main"] + "/" + p["hedge"] + ": " + str(e)[:80])
            continue
        if not ok:
            bot.send_message(chat_id, "🤖 " + p["main"] + "/" + p["hedge"] + " — " + str(info)[:120])
            continue
        free -= 2
        tracked.setdefault(chat_id, [])
        tracked[chat_id].append({
            "kind": "pair", "main": p["main"], "hedge": p["hedge"],
            "side_main": p["side_main"], "rate0": p["rate"], "corr": p["corr"],
            "fa": info["fa"], "fb": info["fb"],
            "bn_qa": info["qa"], "bn_qb": info["qb"],
            "bn_pa": info["pa"], "bn_pb": info["pb"],
            "bn_side_a": info["side_a"], "bn_side_b": info["side_b"],
            "t0_s": time.time(),
            "bn_t": int(time.time() * 1000) - 5 * 60 * 1000,
        })
        save()
        if p["side_main"] == "short":
            legs = "🔴 ШОРТ " + p["main"] + "  🟢 ЛОНГ " + p["hedge"]
        else:
            legs = "🟢 ЛОНГ " + p["main"] + "  🔴 ШОРТ " + p["hedge"]
        bot.send_message(chat_id,
            "🤖 ОТКРЫЛ ФАНДИНГ-ФАРМ: " + p["main"] + " + хедж " + p["hedge"] + "\n"
            + legs + "\n"
            "фандинг " + format(p["rate"], "+.3f") + "% · нетто ~" + format(p["net"], ".3f") + "% · corr " + format(p["corr"], ".2f") + "\n"
            "Собираю фандинг. Руками не трогаем.", reply_markup=menu())
        time.sleep(1)


# ---------- ЗАКРЫТИЕ ПАРЫ + ЗАПИСЬ PnL ----------

def record_pair_close(chat_id, s, reason_ic, reason_txt):
    """
    Обе ноги закрыты. Тянем реальный PnL по обеим монетам с биржи и пишем в историю.
    """
    since = s.get("bn_t", int(time.time() * 1000) - 86400000)
    ia = last_trade_income(s["fa"], since)
    ib = last_trade_income(s["fb"], since)
    net = (ia["pnl"] + ia["fee"] + ia["fund"]) + (ib["pnl"] + ib["fee"] + ib["fund"])
    gross = ia["pnl"] + ib["pnl"]
    fee = ia["fee"] + ib["fee"]
    fund = ia["fund"] + ib["fund"]
    history.setdefault(chat_id, [])
    history[chat_id].append({
        "sym": s["main"] + "+" + s["hedge"], "side": "funding", "result": reason_txt,
        "pnl": round(net, 2), "gross": round(gross, 2),
        "fee": round(fee, 2), "fund": round(fund, 2),
        "src": "binance", "t": int(time.time())})
    save()
    try:
        bot.send_message(chat_id,
            reason_ic + " ФАРМ ЗАКРЫТ: " + s["main"] + " + " + s["hedge"] + "\n"
            "(" + reason_txt + ")\n\n"
            "📈 Грязный: " + money(gross) + "\n"
            "💸 Комиссии: " + money(fee) + "\n"
            "💱 Фандинг: " + money(fund) + "\n"
            "━━━━━━━━━━\n"
            "💵 Чистыми: " + money(net) + "\n\n"
            "Записал в статистику.", reply_markup=menu())
    except Exception:
        pass
    return net


# ---------- ГЛАВНЫЙ ЦИКЛ ----------

def auto_loop():
    last = 0
    while True:
        try:
            changed = False
            for chat_id, lst in list(tracked.items()):
                for s in list(lst):
                    if s.get("kind") != "pair":
                        continue
                    try:
                        rate = live_funding(s)
                        div = legs_divergence(s)
                        s["rate_last"] = round(rate, 4) if rate is not None else None
                        s["div_last"] = round(div, 2)
                        changed = True
                        held_h = (time.time() - s.get("t0_s", time.time())) / 3600
                        reason = None
                        if div >= LEG_STOP:
                            reason = ("🛑", "хедж не держит, ноги разъехались " + format(div, ".1f") + "%")
                        elif rate is not None and abs(rate) < EXIT_FUNDING:
                            reason = ("🎯", "фандинг упал до " + format(rate, "+.3f") + "%, фиксируем")
                        elif held_h >= MAX_HOLD_H:
                            reason = ("⏱", "вышло время " + format(held_h, ".0f") + "ч")
                        if reason:
                            closed_ok = close_pair(s, reason[1])
                            # подстраховка: если обе ноги на бирже уже нулевые —
                            # пара фактически закрыта, убираем из трекинга
                            if not closed_ok:
                                try:
                                    pos = binance_positions()
                                    gone = (abs(pos.get(s["fa"], {}).get("amt", 0)) <= 0 and
                                            abs(pos.get(s["fb"], {}).get("amt", 0)) <= 0)
                                except Exception:
                                    gone = False
                                closed_ok = gone
                            if closed_ok:
                                record_pair_close(chat_id, s, reason[0], reason[1])
                                lst.remove(s)
                                changed = True
                            else:
                                bot.send_message(chat_id, "⚠️ " + s["main"] + "/" + s["hedge"]
                                                 + ": не смог закрыть обе ноги, проверь биржу.")
                    except Exception as e:
                        print("pair loop:", str(e)[:80])
                    time.sleep(0.4)
            if changed:
                save()
            if time.time() - last > SCAN_EVERY:
                last = time.time()
                for uid in list(users):
                    try:
                        do_scan(uid, manual=False)
                    except Exception as e:
                        print("автоскан error:", e)
        except Exception as e:
            print("loop error:", e)
        time.sleep(60)


# ---------- ЗАПУСК ----------

load()
if CHAT_ID:
    try:
        users.add(int(CHAT_ID)); save()
    except Exception:
        pass
print("получателей:", len(users), "| CHAT_ID:", CHAT_ID or "НЕ ЗАДАН")

if has_keys():
    sync_time()
    load_filters()
    try:
        pp = binance_positions()
        print("Binance OK, открытых ног:", len(pp))
    except Exception as e:
        print("Binance ключи не работают:", e)
    print("ПАРЫ | авто:", "ВКЛ" if auto_on else "выкл",
          "| мин.оценка", AUTO_MIN_PTS, "| потолок ног", MAX_POS, "| плечо", LEVERAGE,
          "| фандинг≥", MIN_FUNDING, "| выход<", EXIT_FUNDING, "| стоп ног", LEG_STOP,
          "| corr≥", MIN_CORR)
else:
    print("Binance ключи не заданы")

_ok = None
for _h in DATA_HOSTS:
    try:
        _p = "/api/v3/ticker/price?symbol=BTCUSDT"
        if "fapi.binance.com" in _h:
            _p = _p.replace("/api/v3/", "/fapi/v1/")
        if requests.get(_h + _p, timeout=10).status_code == 200:
            _ok = _h; _host_idx["i"] = DATA_HOSTS.index(_h); break
    except Exception:
        pass
print("источник данных:", _ok or "НИ ОДИН!")

threading.Thread(target=auto_loop, daemon=True).start()
print("Парный бот запущен. Автоторговля:", "ВКЛ" if auto_on else "ВЫКЛ")

try:
    bot.remove_webhook(); time.sleep(1)
except Exception as e:
    print("remove_webhook:", e)

while True:
    try:
        bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    except Exception as e:
        msg = str(e)
        if "409" in msg or "Conflict" in msg:
            print("409: другой экземпляр ещё жив, жду 15с...")
            time.sleep(15)
        else:
            print("polling error:", msg[:200])
            time.sleep(5)
