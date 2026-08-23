import os
import time
import requests
from datetime import datetime, timezone

# ==================== КОНФИГ ====================
PROXY = "https://bybit-proxy.aleks000009.workers.dev"
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# --- отбор монет ---
MIN_TURNOVER_24H = 2_000_000   # лёгкий фильтр от неликвида. 0 = ВСЕ монеты

# --- уровни (как на сайте: max HIGH / min LOW за N свечей на 4ч и 1д) ---
BARS_BACK = 100                # окно для экстремума (как findLevels на сайте)
LEVEL_LIMIT = 150              # сколько свечей тянуть
REFRESH_LEVELS_SEC = 3600      # пересобирать базу раз в час

# таймфреймы уровней: (interval, подпись)
LEVEL_TFS = [("240", "4ч"), ("D", "1д")]

# --- радар подхода ---
APPROACH_PCT = 0.007           # алерт, когда цена в пределах 0.7% от уровня
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
            out[sym] = price
    return out


def get_hl(symbol, interval, limit):
    """Возвращает (max_high, min_low) за последние BARS_BACK закрытых свечей."""
    try:
        url = (f"{PROXY}/kline?category=linear&symbol={symbol}"
               f"&interval={interval}&limit={limit}")
        r = requests.get(url, timeout=20)
        raw = r.json()["result"]["list"]
    except Exception:
        return None
    raw = raw[::-1]              # новые -> в хронологический порядок
    raw = raw[:-1]              # только закрытые свечи
    if len(raw) < 5:
        return None
    sl = raw[-BARS_BACK:] if len(raw) > BARS_BACK else raw
    hh = max(float(k[2]) for k in sl)
    ll = min(float(k[3]) for k in sl)
    return hh, ll


# ==================== БАЗА УРОВНЕЙ ====================
def build_levels(symbols):
    db = {}
    for sym in symbols:
        entry = {}
        for interval, label in LEVEL_TFS:
            hl = get_hl(sym, interval, LEVEL_LIMIT)
            time.sleep(REQUEST_SLEEP)
            if hl:
                entry[label] = hl   # (hh, ll)
        if entry:
            db[sym] = entry
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


def evaluate(price, entry):
    """Ближайший глобальный уровень (max/min за 100 свечей на 4ч/1д) в зоне подхода."""
    best = None
    best_dist = APPROACH_PCT + 1

    for label, (hh, ll) in entry.items():
        # сопротивление = экстремум-хай ВЫШЕ цены
        if hh > price:
            d = (hh - price) / price
            if d <= APPROACH_PCT and d < best_dist:
                best_dist = d
                best = (f"{label} сопротивление ↑", hh, d)
        # поддержка = экстремум-лоу НИЖЕ цены
        if ll < price:
            d = (price - ll) / price
            if d <= APPROACH_PCT and d < best_dist:
                best_dist = d
                best = (f"{label} поддержка ↓", ll, d)

    return best


def build_alert(symbol, price, info):
    kind, lvl, dist = info
    return (
        f"🎯 <b>ПОДХОД К УРОВНЮ</b> — {symbol}\n\n"
        f"Цена {fmt(price)} → {kind} <b>{fmt(lvl)}</b> ({dist*100:.2f}%)\n\n"
        f"👉 Смотри стакан: плотность / закол / срыв стопов"
    )


# ==================== MAIN ====================
def main():
    tg("📡 Радар глобальных уровней запущен (max/min 100 свечей · 4ч + 1д)")
    levels_db = {}
    last_build = 0
    last_alert = {}

    while True:
        tickers = get_tickers()
        symbols = list(tickers.keys())
        now = time.time()

        if now - last_build > REFRESH_LEVELS_SEC or not levels_db:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Строю базу уровней по {len(symbols)} монетам...")
            levels_db = build_levels(symbols)
            last_build = time.time()
            print(f"База готова: {len(levels_db)} монет")

        alerts = 0
        for sym in symbols:
            entry = levels_db.get(sym)
            if not entry:
                continue
            price = tickers[sym]
            info = evaluate(price, entry)
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
