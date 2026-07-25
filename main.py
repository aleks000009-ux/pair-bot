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

SIZE = 1000                                             # номинал КАЖДОЙ ноги (лонг и шорт по $SIZE)
# --- ПАРНЫЙ ТРЕЙДИНГ (классика z-score) ---
TF = os.environ.get("PAIR_TF", "1h")                    # таймфрейм расчёта спреда
ZWIN = int(os.environ.get("ZWIN", "100"))               # окно скользящего среднего спреда (свечей)
Z_ENTRY = float(os.environ.get("Z_ENTRY", "2.0"))       # вход когда |z| >= этого
Z_EXIT = float(os.environ.get("Z_EXIT", "0.3"))         # выход когда |z| <= этого (спред сошёлся)
Z_STOP = float(os.environ.get("Z_STOP", "3.5"))         # стоп когда |z| >= этого (разъехались дальше)
MIN_CORR = float(os.environ.get("MIN_CORR", "0.8"))     # минимальная корреляция пары для торговли
CORR_WIN = int(os.environ.get("CORR_WIN", "200"))       # окно расчёта корреляции (свечей)
MAX_HOLD_H = float(os.environ.get("MAX_HOLD_H", "72"))  # макс. время удержания пары, часов (защита от «залипших»)
CAND_COINS = int(os.environ.get("CAND_COINS", "40"))    # из скольких топ-монет строим пары
MAX_RISK = float(os.environ.get("MAX_RISK", "3.0"))     # оставлено для совместимости хелперов
FEE = 0.055
BE_BAND = 5.0
BTC_FLAT = 0.7
BTC_FILTER = False                                      # к парам не применяется
SCAN_EVERY = int(os.environ.get("SCAN_EVERY", "1800"))   # период автоскана, сек

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



# ======================= ПАРНЫЙ ТРЕЙДИНГ =======================
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
    for x in usdt[:CAND_COINS + 30]:
        sym = x["symbol"].replace("USDT", "")
        if sym.upper() in BLACKLIST or x["symbol"].upper() in BLACKLIST:
            continue
        if sym + "USDT" not in _filters:      # только те, что реально торгуются фьючерсом
            continue
        out.append(sym)
        if len(out) >= CAND_COINS:
            break
    return out


def closes_tf(sym, limit):
    """закрытия свечей TF по монете; None при ошибке"""
    try:
        kl = get_klines_tf(sym + "USDT", TF, limit)
    except Exception:
        return None
    if not kl or len(kl) < limit // 2:
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


_price_cache = {}


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
    Строим пары из топ-CAND_COINS монет, считаем корреляцию и z-score.
    Возвращаем список готовых к входу пар (|z|>=Z_ENTRY, corr>=MIN_CORR),
    отсортированных по |z| убыв.
    """
    funnel.clear()
    coins = top_coins()[:CAND_COINS]
    # тянем закрытия один раз на монету
    data = {}
    need = max(ZWIN, CORR_WIN) + 5
    for c in coins:
        if c.upper() in BLACKLIST:
            continue
        cl = closes_tf(c, need)
        if cl and len(cl) >= need:
            data[c] = cl
        time.sleep(0.05)
    syms = list(data.keys())
    fn("монет с данными")
    found = []
    seen = 0
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            seen += 1
            corr = correlation(data[a], data[b])
            if corr < MIN_CORR:
                continue
            fn("коррелируют")
            # КОИНТЕГРАЦИЯ: отсеиваем ложные пары (совместный рост с рынком),
            # оставляем только те, где спред реально тянет к среднему
            coint, hl = is_cointegrated(data[a], data[b])
            if not coint:
                fn("не коинтегрированы")
                continue
            fn("коинтегрированы")
            sp = pair_spread(data[a], data[b])
            if not sp:
                continue
            z, m, sd = zscore(sp)
            if z is None:
                continue
            if abs(z) < Z_ENTRY:
                fn("z мал")
                continue
            fn("готовы к входу")
            # z>0: спред A-B высок -> A дорогая -> шорт A, лонг B
            if z > 0:
                strong, weak = a, b
            else:
                strong, weak = b, a
            found.append({"a": a, "b": b, "strong": strong, "weak": weak,
                          "z": z, "corr": corr, "half_life": hl})
    funnel["пар проверено"] = seen
    found.sort(key=lambda x: -abs(x["z"]))
    return found


def grade_pair(p):
    """оценка пары 0..6 — чем выше corr и |z|, тем лучше"""
    pts = 0
    az = abs(p["z"])
    if az >= 3.0: pts += 2
    elif az >= 2.5: pts += 1
    if p["corr"] >= 0.9: pts += 2
    elif p["corr"] >= 0.85: pts += 1
    if az >= 2.2: pts += 1
    if p["corr"] >= 0.95: pts += 1
    if pts >= 5: return pts, "🟢 СИЛЬНАЯ ПАРА"
    if pts >= 3: return pts, "🟡 СРЕДНЯЯ ПАРА"
    return pts, "🔴 СЛАБАЯ ПАРА"


def pair_card(p):
    pts, lab = grade_pair(p)
    return (
        "🔗 " + p["a"] + " / " + p["b"] + "\n"
        + lab + "  (" + str(pts) + "/6)\n\n"
        "📊 z-score: " + format(p["z"], "+.2f") + "  (вход при |z|≥" + str(Z_ENTRY) + ")\n"
        "🔀 корреляция: " + format(p["corr"], ".2f") + "\n"
        "🔁 коинтеграция ✅ · полураспад " + (format(p.get("half_life", 0), ".0f") + " св." if p.get("half_life") else "?") + "\n\n"
        "🔴 ШОРТ " + p["strong"] + "  (сильная, ждём падения)\n"
        "🟢 ЛОНГ " + p["weak"] + "  (слабая, ждём роста)\n\n"
        "Ставка на схождение спреда.\n"
        "Выход при |z|≤" + str(Z_EXIT) + " (профит) или |z|≥" + str(Z_STOP) + " (стоп).\n"
        "⚙️ по $" + str(SIZE) + " на ногу · плечо " + str(LEVERAGE) + "x"
    )


# ---------- ИСПОЛНЕНИЕ ПАРЫ (две ноги рынком) ----------

def _market_leg(fs, side, qty):
    return signed_post("/fapi/v1/order", {"symbol": fs, "side": side,
                                          "type": "MARKET", "quantity": qty})


def open_pair(chat_id, p):
    """
    Открываем обе ноги рыночными ордерами.
    Если вторая нога не встала — первую немедленно закрываем (защита от одной ноги).
    """
    fa = auto_symbol(p["weak"])     # лонг слабой
    fb = auto_symbol(p["strong"])   # шорт сильной
    if not fa or not fb:
        return False, "нет фьючерса на одну из монет"
    key = fa + "_" + fb
    if key in opened_keys:
        return False, "уже открывали"

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
        return False, p["weak"] + ": размер меньше минимума"
    if qb < ffb["minQty"] or qb * pb < ffb["minNot"]:
        return False, p["strong"] + ": размер меньше минимума"

    # нога 1 — лонг слабой
    try:
        _market_leg(fa, "BUY", qa)
    except Exception as e:
        return False, "нога 1 (" + p["weak"] + ") не встала: " + str(e)

    # нога 2 — шорт сильной; если не встала, откатываем ногу 1
    try:
        _market_leg(fb, "SELL", qb)
    except Exception as e:
        try:
            close_now(fa, "BUY", qa)   # закрываем лонг
            return False, ("нога 2 (" + p["strong"] + ") не встала, ногу 1 закрыл: " + str(e))
        except Exception as e2:
            bot.send_message(chat_id, "🆘 " + p["weak"] + " ОСТАЛАСЬ ОДНОЙ НОГОЙ, закрыть не смог: "
                             + str(e2) + "\nЗАКРОЙ РУКАМИ.")
            return False, "одна нога, закрыть не удалось"

    opened_keys.add(key)

    # АВАРИЙНЫЕ СТОПЫ на бирже: если бот умрёт, ноги не останутся без защиты.
    # Ставим ШИРОКО (EMERG_STOP % от входа) — это не торговый стоп по z,
    # а страховка от неконтролируемого убытка при падении процесса.
    emerg = float(os.environ.get("EMERG_STOP", "8.0")) / 100
    sl_msgs = []
    # лонг слабой -> аварийный стоп снизу
    try:
        sla = step_round(pa * (1 - emerg), ffa["tick"])
        place_cond(fa, "SELL", "STOP_MARKET", sla)
    except Exception as e:
        sl_msgs.append(p["weak"] + " SL: " + str(e)[:50])
    # шорт сильной -> аварийный стоп сверху
    try:
        slb = step_round(pb * (1 + emerg), ffb["tick"])
        place_cond(fb, "BUY", "STOP_MARKET", slb)
    except Exception as e:
        sl_msgs.append(p["strong"] + " SL: " + str(e)[:50])
    if sl_msgs:
        try:
            bot.send_message(chat_id, "⚠️ пара открыта, но аварийный стоп не встал:\n"
                             + "\n".join(sl_msgs) + "\nВыход по z работает, но при падении бота защиты нет.")
        except Exception:
            pass

    return True, {"fa": fa, "fb": fb, "qa": qa, "qb": qb, "pa": pa, "pb": pb}


def close_pair(s, reason):
    """закрываем обе ноги пары рыночными reduceOnly + снимаем аварийные стопы"""
    ok = True
    # сначала снимаем аварийные стопы, чтобы не сработали по закрытой ноге
    for fs in (s["fa"], s["fb"]):
        try:
            cancel_algo(fs)
        except Exception as e:
            print("cancel emerg", fs, ":", str(e)[:60])
    try:
        close_now(s["fa"], "BUY", s["bn_qa"])     # закрыть лонг слабой
    except Exception as e:
        print("close leg A:", e); ok = False
    try:
        close_now(s["fb"], "SELL", s["bn_qb"])    # закрыть шорт сильной
    except Exception as e:
        print("close leg B:", e); ok = False
    opened_keys.discard(s["fa"] + "_" + s["fb"])
    return ok


# ---------- МЕНЮ ----------

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти пары")
    kb.row("📋 Мои пары", "📊 Статистика")
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
        "Парный трейдинг 🔗\n"
        "Ищу коррелированные пары (corr≥" + str(MIN_CORR) + "), торгую расхождение спреда.\n"
        "Вход при |z|≥" + str(Z_ENTRY) + ", выход при |z|≤" + str(Z_EXIT)
        + " (профит) или |z|≥" + str(Z_STOP) + " (стоп).\n\n" + k, reply_markup=menu())


@bot.message_handler(func=lambda m: m.text == "🔍 Найти пары")
def btn_scan(m):
    users.add(m.chat.id)
    save()
    bot.send_message(m.chat.id, "Ищу пары среди топ-" + str(CAND_COINS) + " монет на " + TF + "... жди 2-4 мин")
    do_scan(m.chat.id, manual=True)


@bot.message_handler(func=lambda m: m.text == "📋 Мои пары")
def btn_list(m):
    t = [x for x in tracked.get(m.chat.id, []) if x.get("kind") == "pair"]
    if not t:
        bot.send_message(m.chat.id, "Нет открытых пар.", reply_markup=menu())
        return
    for s in list(t):
        try:
            z = live_z(s)
            zt = format(z, "+.2f") if z is not None else "?"
            held = (time.time() - s.get("t0", time.time()) / 1) / 3600
            held_h = (time.time() - s.get("t0_s", time.time())) / 3600
            txt = (
                "🔗 " + s["a"] + " / " + s["b"] + "\n\n"
                "🔴 ШОРТ " + s["strong"] + "\n"
                "🟢 ЛОНГ " + s["weak"] + "\n\n"
                "📊 z сейчас: " + zt + "  (вход был " + format(s["z0"], "+.2f") + ")\n"
                "🎯 выход при |z|≤" + str(Z_EXIT) + " · стоп при |z|≥" + str(Z_STOP) + "\n"
                "⏱ в позиции " + format(held_h, ".1f") + " ч"
            )
            bot.send_message(m.chat.id, txt, reply_markup=menu())
        except Exception as e:
            bot.send_message(m.chat.id, s["a"] + "/" + s["b"] + ": " + str(e)[:60])
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
        # пара = два закрытия рядом по времени; но для простоты统计 берём разбивку по монетам
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
        L.append("📊 ПАРНЫЙ ТРЕЙДИНГ · с " + STATS_START + " МСК")
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
    order = ["монет с данными", "пар проверено", "коррелируют",
             "не коинтегрированы", "коинтегрированы", "z мал", "готовы к входу"]
    t = "\n📉 ВОРОНКА:"
    for k in order:
        if funnel.get(k):
            t += "\n  " + k + ": " + str(funnel[k])
    return t


# ---------- ТЕКУЩИЙ z ПО ОТКРЫТОЙ ПАРЕ ----------

def live_z(s):
    ca = closes_tf(s["a"], max(ZWIN, CORR_WIN) + 5)
    cb = closes_tf(s["b"], max(ZWIN, CORR_WIN) + 5)
    if not ca or not cb:
        return None
    sp = pair_spread(ca, cb)
    if not sp:
        return None
    z, _, _ = zscore(sp)
    return z


# ---------- СКАН + АВТОВХОД ----------

def do_scan(chat_id, manual=False):
    try:
        pairs = scan_pairs()
        busy_syms = set()
        for x in tracked.get(chat_id, []):
            if x.get("kind") == "pair":
                busy_syms.add(x["a"]); busy_syms.add(x["b"])
        # не берём пары, где монета уже занята другой парой
        fresh = [p for p in pairs if p["a"] not in busy_syms and p["b"] not in busy_syms]

        if auto_on and has_keys():
            auto_enter(chat_id, fresh)

        if not pairs:
            if manual:
                bot.send_message(chat_id, "Разъехавшихся пар сейчас нет.\n"
                                 "(нужна corr≥" + str(MIN_CORR) + " и |z|≥" + str(Z_ENTRY) + ")"
                                 + funnel_text(), reply_markup=menu())
            return

        show = fresh[:6]
        if not show:
            if manual:
                bot.send_message(chat_id, "Готовые пары есть, но их монеты уже в работе."
                                 + funnel_text(), reply_markup=menu())
            return
        head = "Нашёл пар: " + str(len(fresh))
        if manual:
            head += funnel_text()
        bot.send_message(chat_id, head)
        for p in show:
            bot.send_message(chat_id, pair_card(p), reply_markup=menu())
            time.sleep(0.6)
    except Exception as e:
        print("scan error:", e)
        if manual:
            bot.send_message(chat_id, "Ошибка при поиске пар.", reply_markup=menu())


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
        fa = auto_symbol(p["weak"]); fb = auto_symbol(p["strong"])
        if not fa or not fb or fa in pos or fb in pos:
            continue
        try:
            ok, info = open_pair(chat_id, p)
        except Exception as e:
            bot.send_message(chat_id, "🤖 ❌ " + p["a"] + "/" + p["b"] + ": " + str(e)[:80])
            continue
        if not ok:
            bot.send_message(chat_id, "🤖 " + p["a"] + "/" + p["b"] + " — " + str(info)[:120])
            continue
        free -= 2
        tracked.setdefault(chat_id, [])
        tracked[chat_id].append({
            "kind": "pair", "a": p["a"], "b": p["b"],
            "strong": p["strong"], "weak": p["weak"],
            "z0": p["z"], "corr": p["corr"],
            "fa": info["fa"], "fb": info["fb"],
            "bn_qa": info["qa"], "bn_qb": info["qb"],
            "bn_pa": info["pa"], "bn_pb": info["pb"],
            "t0_s": time.time(),
            "bn_t": int(time.time() * 1000) - 5 * 60 * 1000,
        })
        save()
        bot.send_message(chat_id,
            "🤖 ОТКРЫЛ ПАРУ " + p["a"] + " / " + p["b"] + "  " + grade_pair(p)[1] + "\n"
            "🔴 ШОРТ " + p["strong"] + "  🟢 ЛОНГ " + p["weak"] + "\n"
            "z входа: " + format(p["z"], "+.2f") + " · corr " + format(p["corr"], ".2f") + "\n"
            "Выход по схождению спреда. Руками не трогаем.", reply_markup=menu())
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
        "sym": s["a"] + "/" + s["b"], "side": "pair", "result": reason_txt,
        "pnl": round(net, 2), "gross": round(gross, 2),
        "fee": round(fee, 2), "fund": round(fund, 2),
        "src": "binance", "t": int(time.time())})
    try:
        bot.send_message(chat_id,
            reason_ic + " ПАРА ЗАКРЫТА: " + s["a"] + " / " + s["b"] + "\n"
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
                        z = live_z(s)
                        if z is None:
                            continue
                        held_h = (time.time() - s.get("t0_s", time.time())) / 3600
                        reason = None
                        if abs(z) <= Z_EXIT:
                            reason = ("🎯", "спред сошёлся z=" + format(z, "+.2f"))
                        elif abs(z) >= Z_STOP:
                            reason = ("🛑", "разъехались z=" + format(z, "+.2f"))
                        elif held_h >= MAX_HOLD_H:
                            reason = ("⏱", "вышло время " + format(held_h, ".0f") + "ч, z=" + format(z, "+.2f"))
                        if reason:
                            if close_pair(s, reason[1]):
                                record_pair_close(chat_id, s, reason[0], reason[1])
                                lst.remove(s)
                                changed = True
                            else:
                                bot.send_message(chat_id, "⚠️ " + s["a"] + "/" + s["b"]
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
          "| TF", TF, "| z-вход", Z_ENTRY, "| z-выход", Z_EXIT, "| z-стоп", Z_STOP,
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
