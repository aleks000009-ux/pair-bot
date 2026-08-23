import os
import time
import math
import requests
from datetime import datetime, timezone

# ==================== КОНФИГ ====================
PROXY = "https://bybit-proxy.aleks000009.workers.dev"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# --- отбор монет ---
MIN_TURNOVER_24H = 2_000_000   # лёгкий фильтр от неликвида. 0 = ВСЕ монеты

# --- база значимых 4H-уровней (обновляется раз в час) ---
LEVEL_TF = "240"               # 4 часа
LEVEL_LIMIT = 200              # ~33 дня истории
PIVOT_W = 2                    # локальный экстремум: выше/ниже 2 соседей с каждой стороны
PROM_SPAN = 30                 # окно оценки «торчащести» уровня (свечей в каждую сторону)
MIN_PROM = 0.03                # МИН. prominence 3% — уровень должен реально торчать (это и есть «глобальность»)
CLUSTER_TOL = 0.005            # 0.5% — близкие уровни сливаются в один зональный уровень
MAX_LEVELS_PER_SIDE = 8        # держим только N сильнейших уровней на сторону
REFRESH_LEVELS_SEC = 3600      # пересобирать базу раз в час

# --- радар подхода ---
APPROACH_PCT = 0.005           # алерт, когда цена в пределах 0.5% от уровня
CASCADE_RANGE = 0.03           # соседние уровни в пределах 3% = каскад
VOID_TOL = 0.002               # уровень = ATH/ATL если в 0.2% от глобального экстремума
ALERT_MIN_TOUCHES = 1          # мин. касаний, чтобы уровень был «хорошим» (1 = любой сильный свинг)
ALERT_COOLDOWN = 7200          # не повторять один уровень чаще раза в 2ч

REQUEST_SLEEP = 0.12           # пауза между запросами к прокси (только при сборке базы)
LOOP_PAUSE = 30                # цикл радара, сек (лёгкий — только /tickers)


# ==================== TELEGRAM ====================
def tg(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg,
                                 "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print("TG error:", e)


# ==================== ДАННЫЕ ====================
def get_tickers():
    """{symbol: (lastPrice, turnover24h)} по всем USDT-перпам одним запросом."""
    try:
        r = requests.get(f"{PROXY}/tickers?category=linear", timeout=20)
        data = r.json()["result"]["list"]
    except Exception as e:
        print("tickers error:", e)
        return {}
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            price = float(t.get("lastPrice", 0))
            turnover = float(t.get("turnover24h", 0))
        except Exception:
            continue
        if price > 0 and turnover >= MIN_TURNOVER_24H:
            out[sym] = (price, turnover)
    return out


def get_klines(symbol, interval, limit):
    try:
        url = (f"{PROXY}/kline?category=linear&symbol={symbol}"
               f"&interval={interval}&limit={limit}")
        r = requests.get(url, timeout=20)
        raw = r.json()["result"]["list"]
    except Exception:
        return None
    raw = raw[::-1]  # новые свечи -> в хронологический порядок
    candles = [{"high": float(k[2]), "low": float(k[3])} for k in raw]
    return candles[:-1]  # только закрытые свечи


# ==================== PROMINENCE (глобальность уровня) ====================
def prom_high(candles, i, span):
    """Насколько вершина торчит: спад от пика до высшей из окружающих впадин."""
    h = candles[i]["high"]
    left_min = h
    j = i - 1
    while j >= 0 and i - j <= span:
        if candles[j]["high"] > h:
            break
        left_min = min(left_min, candles[j]["low"])
        j -= 1
    right_min = h
    j = i + 1
    while j < len(candles) and j - i <= span:
        if candles[j]["high"] > h:
            break
        right_min = min(right_min, candles[j]["low"])
        j += 1
    col = max(left_min, right_min)
    return (h - col) / h if h > 0 else 0


def prom_low(candles, i, span):
    """Насколько впадина торчит вниз."""
    l = candles[i]["low"]
    left_max = l
    j = i - 1
    while j >= 0 and i - j <= span:
        if candles[j]["low"] < l:
            break
        left_max = max(left_max, candles[j]["high"])
        j -= 1
    right_max = l
    j = i + 1
    while j < len(candles) and j - i <= span:
        if candles[j]["low"] < l:
            break
        right_max = max(right_max, candles[j]["high"])
        j += 1
    col = min(left_max, right_max)
    return (col - l) / l if l > 0 else 0


def find_levels(candles, kind):
    """Находит значимые (торчащие) уровни: [(price, touches, prominence), ...]."""
    w = PIVOT_W
    n = len(candles)
    picks = []  # (price, prominence)
    for i in range(w, n - w):
        window = candles[i - w:i + w + 1]
        if kind == "high" and candles[i]["high"] == max(c["high"] for c in window):
            p = prom_high(candles, i, PROM_SPAN)
            if p >= MIN_PROM:
                picks.append((candles[i]["high"], p))
        if kind == "low" and candles[i]["low"] == min(c["low"] for c in window):
            p = prom_low(candles, i, PROM_SPAN)
            if p >= MIN_PROM:
                picks.append((candles[i]["low"], p))

    if not picks:
        return []

    # кластеризуем близкие уровни в зоны
    picks.sort(key=lambda x: x[0])
    clusters = [[picks[0]]]
    for pr in picks[1:]:
        if (pr[0] - clusters[-1][-1][0]) / clusters[-1][-1][0] <= CLUSTER_TOL:
            clusters[-1].append(pr)
        else:
            clusters.append([pr])

    levels = []
    for cl in clusters:
        price = sum(x[0] for x in cl) / len(cl)
        touches = len(cl)
        prominence = max(x[1] for x in cl)
        levels.append((price, touches, prominence))

    # оставляем N сильнейших по (prominence * touches)
    levels.sort(key=lambda x: x[2] * x[1], reverse=True)
    return levels[:MAX_LEVELS_PER_SIDE]


def build_levels(symbols):
    db = {}
    for sym in symbols:
        c = get_klines(sym, LEVEL_TF, LEVEL_LIMIT)
        time.sleep(REQUEST_SLEEP)
        if not c or len(c) < 60:
            continue
        db[sym] = {
            "res": find_levels(c, "high"),
            "sup": find_levels(c, "low"),
            "high_all": max(x["high"] for x in c),
            "low_all": min(x["low"] for x in c),
        }
    return db


# ==================== ХЕЛПЕРЫ ====================
def fmt(p):
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.7f}"


def is_round(level):
    if level <= 0:
        return False
    exp = math.floor(math.log10(level))
    base = 10 ** exp
    for step in (base, base / 2):
        r = level / step
        if abs(r - round(r)) < 0.01:
            return True
    return False


def evaluate(price, db):
    """Лучший глобальный уровень в зоне подхода или None."""
    best = None
    best_score = -1

    for lvl, touches, prom in db["res"]:
        dist = (lvl - price) / price
        if not (0 <= dist <= APPROACH_PCT):
            continue
        cascade_n = sum(1 for p, _, _ in db["res"]
                        if lvl * 1.0005 < p <= lvl * (1 + CASCADE_RANGE))
        void = lvl >= db["high_all"] * (1 - VOID_TOL)
        rnd = is_round(lvl)
        if not (touches >= ALERT_MIN_TOUCHES or void or cascade_n >= 1):
            continue
        score = prom * 100 + touches + (3 if void else 0) + 2 * cascade_n
        if score > best_score:
            best_score = score
            best = ("сопротивление ↑", lvl, dist, touches, prom, cascade_n, void, rnd)

    for lvl, touches, prom in db["sup"]:
        dist = (price - lvl) / price
        if not (0 <= dist <= APPROACH_PCT):
            continue
        cascade_n = sum(1 for p, _, _ in db["sup"]
                        if lvl * (1 - CASCADE_RANGE) <= p < lvl * 0.9995)
        void = lvl <= db["low_all"] * (1 + VOID_TOL)
        rnd = is_round(lvl)
        if not (touches >= ALERT_MIN_TOUCHES or void or cascade_n >= 1):
            continue
        score = prom * 100 + touches + (3 if void else 0) + 2 * cascade_n
        if score > best_score:
            best_score = score
            best = ("поддержка ↓", lvl, dist, touches, prom, cascade_n, void, rnd)

    return best


def build_alert(symbol, price, info):
    kind, lvl, dist, touches, prom, cascade_n, void, rnd = info
    tags = []
    if void:
        tags.append("🌌 ПУСТОТА за уровнем (ATH/ATL)")
    if cascade_n >= 1:
        tags.append(f"⛓ Каскад: ещё {cascade_n} уровн. за ним")
    if rnd:
        tags.append("🔢 Круглый уровень")
    tag_block = ("\n" + "\n".join(tags)) if tags else ""

    return (
        f"🎯 <b>ПОДХОД К УРОВНЮ</b> — {symbol}\n\n"
        f"Цена {fmt(price)} → {kind} <b>{fmt(lvl)}</b> ({dist*100:.2f}%)\n"
        f"Сила уровня: {prom*100:.0f}% | касаний: {touches}"
        f"{tag_block}\n\n"
        f"👉 Смотри стакан: плотность / закол / срыв стопов"
    )


# ==================== MAIN ====================
def main():
    tg("📡 Радар глобальных уровней запущен")
    levels_db = {}
    last_build = 0
    last_alert = {}

    while True:
        tickers = get_tickers()
        symbols = list(tickers.keys())
        now = time.time()

        if now - last_build > REFRESH_LEVELS_SEC or not levels_db:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Строю базу глобальных 4H-уровней по {len(symbols)} монетам...")
            levels_db = build_levels(symbols)
            last_build = time.time()
            print(f"База готова: {len(levels_db)} монет")

        alerts = 0
        for sym in symbols:
            db = levels_db.get(sym)
            if not db:
                continue
            price = tickers[sym][0]
            info = evaluate(price, db)
            if not info:
                continue

            lvl = info[1]
            key = f"{sym}:{lvl:.6g}"
            if now - last_alert.get(key, 0) < ALERT_COOLDOWN:
                continue
            last_alert[key] = now
            tg(build_alert(sym, price, info))
            alerts += 1
            print("ALERT:", sym, fmt(lvl))

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] Алертов за цикл: {alerts}")
        time.sleep(LOOP_PAUSE)


if __name__ == "__main__":
    main()
