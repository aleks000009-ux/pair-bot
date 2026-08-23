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
SWING_W = 3                    # окно свинга (макс/мин среди 3 свечей слева и справа)
CLUSTER_TOL = 0.003            # 0.3% — близкие уровни сливаются в один
REFRESH_LEVELS_SEC = 3600      # пересобирать базу раз в час

# --- радар подхода ---
APPROACH_PCT = 0.005           # алерт, когда цена в пределах 0.5% от уровня
CASCADE_RANGE = 0.03           # соседние уровни в пределах 3% = каскад
VOID_TOL = 0.002               # уровень = ATH/ATL если в 0.2% от глобального экстремума
ALERT_MIN_TOUCHES = 1          # мин. касаний, чтобы уровень был «хорошим» (1 = любой чёткий 4H-свинг)
ALERT_COOLDOWN = 7200          # не повторять один и тот же уровень чаще, чем раз в 2ч

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
    candles = []
    for k in raw:
        candles.append({
            "high": float(k[2]),
            "low": float(k[3]),
        })
    return candles[:-1]  # только закрытые свечи


# ==================== 4H-УРОВНИ ====================
def swings(candles, w, kind):
    vals = []
    n = len(candles)
    for i in range(w, n - w):
        window = candles[i - w:i + w + 1]
        if kind == "high" and candles[i]["high"] == max(c["high"] for c in window):
            vals.append(candles[i]["high"])
        if kind == "low" and candles[i]["low"] == min(c["low"] for c in window):
            vals.append(candles[i]["low"])
    return vals


def cluster_levels(prices, tol):
    if not prices:
        return []
    prices = sorted(prices)
    clusters = [[prices[0]]]
    for p in prices[1:]:
        if (p - clusters[-1][-1]) / clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def build_levels(symbols):
    db = {}
    for sym in symbols:
        c = get_klines(sym, LEVEL_TF, LEVEL_LIMIT)
        time.sleep(REQUEST_SLEEP)
        if not c or len(c) < 40:
            continue
        db[sym] = {
            "res": cluster_levels(swings(c, SWING_W, "high"), CLUSTER_TOL),
            "sup": cluster_levels(swings(c, SWING_W, "low"), CLUSTER_TOL),
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
    """Психологически круглый уровень (консервативно)."""
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
    """Возвращает лучший уровень в зоне подхода или None.
    Уровень «годен», если касаний >= ALERT_MIN_TOUCHES ИЛИ есть пустота/каскад.
    Среди годных выбираем самый значимый (score) — просто чтобы не слать дубли по монете."""
    best = None
    best_score = -1

    # --- сопротивление сверху ---
    for lvl, touches in db["res"]:
        dist = (lvl - price) / price
        if not (0 <= dist <= APPROACH_PCT):
            continue
        cascade_n = sum(1 for p, _ in db["res"]
                        if lvl * 1.0005 < p <= lvl * (1 + CASCADE_RANGE))
        void = lvl >= db["high_all"] * (1 - VOID_TOL)
        rnd = is_round(lvl)
        if not (touches >= ALERT_MIN_TOUCHES or void or cascade_n >= 1):
            continue
        score = touches + (3 if void else 0) + 2 * cascade_n + (1 if rnd else 0)
        if score > best_score:
            best_score = score
            best = ("сопротивление ↑", lvl, dist, touches, cascade_n, void, rnd)

    # --- поддержка снизу ---
    for lvl, touches in db["sup"]:
        dist = (price - lvl) / price
        if not (0 <= dist <= APPROACH_PCT):
            continue
        cascade_n = sum(1 for p, _ in db["sup"]
                        if lvl * (1 - CASCADE_RANGE) <= p < lvl * 0.9995)
        void = lvl <= db["low_all"] * (1 + VOID_TOL)
        rnd = is_round(lvl)
        if not (touches >= ALERT_MIN_TOUCHES or void or cascade_n >= 1):
            continue
        score = touches + (3 if void else 0) + 2 * cascade_n + (1 if rnd else 0)
        if score > best_score:
            best_score = score
            best = ("поддержка ↓", lvl, dist, touches, cascade_n, void, rnd)

    return best


def build_alert(symbol, price, info):
    kind, lvl, dist, touches, cascade_n, void, rnd = info
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
        f"Значимость 4H: {touches} касаний"
        f"{tag_block}\n\n"
        f"👉 Смотри стакан: плотность / закол / срыв стопов"
    )


# ==================== MAIN ====================
def main():
    tg("📡 Радар уровней запущен (подход к значимым 4H-уровням по всем монетам)")
    levels_db = {}
    last_build = 0
    last_alert = {}  # "symbol:level" -> timestamp

    while True:
        tickers = get_tickers()
        symbols = list(tickers.keys())
        now = time.time()

        # раз в час пересобираем базу уровней
        if now - last_build > REFRESH_LEVELS_SEC or not levels_db:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Строю базу 4H-уровней по {len(symbols)} монетам...")
            levels_db = build_levels(symbols)
            last_build = time.time()
            print(f"База готова: {len(levels_db)} монет")

        # радар: только цены из tickers, без запросов на монету
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
