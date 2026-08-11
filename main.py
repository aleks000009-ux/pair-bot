#!/usr/bin/env python3
"""
СКАНЕР УЗКИХ КОРИДОРОВ (боковик/консолидация)
Находит монеты, зажатые в узком горизонтальном коридоре, и шлёт в Телеграм:
  - границы коридора (низ/верх)
  - ширину в %
  - сколько времени монета в этом коридоре
  - как быстро проходит от одной границы к другой
Для торговли от границ: покупка у низа, продажа у верха (пила).
НЕ торгует. Bybit-данные через прокси.
"""
import os, time, logging, requests
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "3")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "0"))       # 0 = ВСЕ монеты (не ограничивать)
WINDOW      = int(os.environ.get("WINDOW", "20"))      # окно анализа, свечей 15м
MIN_WIDTH   = float(os.environ.get("MIN_WIDTH", "1.0"))# мин. ширина коридора %
MAX_WIDTH   = float(os.environ.get("MAX_WIDTH", "5.0"))# макс. ширина %
MIN_TOUCHES = int(os.environ.get("MIN_TOUCHES", "2"))  # мин. касаний каждой границы
MIN_TIME_MIN= int(os.environ.get("MIN_TIME_MIN", "60"))# мин. сколько времени в коридоре (мин)
SCAN_EVERY  = int(os.environ.get("SCAN_EVERY", "600"))
COOLDOWN    = int(os.environ.get("COOLDOWN", "3600"))
last_signal = {}

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN/CHAT_ID не заданы — вывод в лог")
        log.info(text.replace("<b>","").replace("</b>","")); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}, timeout=15)
        if r.status_code != 200: log.error(f"TG {r.status_code}: {r.text[:200]}")
    except Exception as e: log.error(f"TG fail: {e}")

def api_get(path, retries=3):
    for i in range(retries):
        try:
            r = requests.get(API + path, timeout=15)
            if r.status_code == 200: return r.json()
        except Exception as e: log.warning(f"fetch {i+1}/{retries}: {e}")
        time.sleep(2*(i+1))
    return None

def get_klines(symbol, interval, limit):
    d = api_get(f"/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}")
    if not d or d.get("retCode") != 0 or not d.get("result", {}).get("list"): return None
    return [{"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])}
            for c in reversed(d["result"]["list"])]

def get_liquid_symbols():
    d = api_get("/tickers?category=linear")
    if not d or d.get("retCode") != 0: return []
    rows = []
    for t in d["result"]["list"]:
        if not t["symbol"].endswith("USDT"): continue
        try:
            turn = float(t.get("turnover24h") or 0)
            if turn < MIN_VOL_USD: continue
            rows.append((t["symbol"], turn))
        except (TypeError, ValueError): continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in (rows if TOP_N <= 0 else rows[:TOP_N])]

def detect_range(candles):
    N = WINDOW
    if len(candles) < N + 5: return None
    zone = candles[-N:]
    hi = max(c['h'] for c in zone)
    lo = min(c['l'] for c in zone)
    mid = (hi + lo) / 2
    if mid <= 0: return None
    width_pct = (hi - lo) / mid * 100
    if not (MIN_WIDTH <= width_pct <= MAX_WIDTH): return None
    band = (hi - lo) * 0.15
    touch_hi = sum(1 for c in zone if c['h'] >= hi - band)
    touch_lo = sum(1 for c in zone if c['l'] <= lo + band)
    if touch_hi < MIN_TOUCHES or touch_lo < MIN_TOUCHES: return None
    q = max(1, N // 4)
    first_q = sum(c['c'] for c in zone[:q]) / q
    last_q  = sum(c['c'] for c in zone[-q:]) / q
    drift = abs(last_q - first_q) / mid * 100
    if drift > width_pct * 0.6: return None  # это тренд, не коридор
    # время в коридоре
    bars_in_range = N
    for i in range(len(candles) - N - 1, -1, -1):
        c = candles[i]
        if lo <= c['l'] and c['h'] <= hi:
            bars_in_range += 1
        else:
            break
    time_in_range_min = bars_in_range * 15
    if time_in_range_min < MIN_TIME_MIN: return None
    # скорость: проходы через среднюю линию
    crossings = 0
    prev_above = zone[0]['c'] > mid
    for c in zone[1:]:
        above = c['c'] > mid
        if above != prev_above:
            crossings += 1; prev_above = above
    speed_min = (N / crossings * 15) if crossings else N * 15
    price = candles[-1]['c']
    pos = (price - lo) / (hi - lo) * 100 if hi > lo else 50  # где сейчас цена в коридоре, %
    return {'high':hi,'low':lo,'mid':mid,'width_pct':width_pct,
            'time_in_range_min':time_in_range_min,'bars_in_range':bars_in_range,
            'touch_hi':touch_hi,'touch_lo':touch_lo,'crossings':crossings,
            'speed_min':speed_min,'price':price,'pos':pos}

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def fmt_time(minutes):
    if minutes < 60: return f"{minutes}мин"
    h = minutes // 60; m = minutes % 60
    return f"{h}ч {m}мин" if m else f"{h}ч"

def signal_text(sym, r):
    s = sym.replace("USDT","")
    # где цена сейчас — подсказка что делать
    if r['pos'] <= 25:
        hint = "🟢 Цена у НИЗА коридора — зона покупки (лонг к верху)"
    elif r['pos'] >= 75:
        hint = "🔴 Цена у ВЕРХА коридора — зона продажи (шорт к низу)"
    else:
        hint = "⚪ Цена в середине — жди подхода к границе"
    return (f"📊 <b>КОРИДОР: {s}/USDT</b>\n\n"
            f"⬆️ Верх: {fp(r['high'])}\n"
            f"⬇️ Низ:  {fp(r['low'])}\n"
            f"📏 Ширина: {r['width_pct']:.2f}%\n"
            f"⏱ В коридоре: {fmt_time(r['time_in_range_min'])}\n"
            f"🔄 Проход между границами: ~{fmt_time(int(r['speed_min']))}\n"
            f"📍 Сейчас цена: {fp(r['price'])} ({r['pos']:.0f}% от низа)\n\n"
            f"{hint}\n\n"
            f"⚠️ Торговля от границ: покупка у низа, продажа у верха. "
            f"Стоп ЗА границу коридора — если пробьёт, боковик кончился, выходи. "
            f"Комиссии съедают часть узкого хода, считай прибыль после них.\n"
            f"📊 https://www.bybit.com/trade/usdt/{sym}?interval=15")

def run():
    log.info("🤖 Сканер узких коридоров запущен")
    log.info(f"Монеты: {'ВСЕ' if TOP_N<=0 else TOP_N} | ширина {MIN_WIDTH}-{MAX_WIDTH}% | окно {WINDOW}св | мин.время {MIN_TIME_MIN}мин | касаний>={MIN_TOUCHES}")
    send_tg(f"🤖 <b>Сканер коридоров запущен</b>\nИщу монеты в узком боковике (ширина {MIN_WIDTH}-{MAX_WIDTH}%).\nДам границы, ширину, время в коридоре и скорость хода.\nСкан каждые {SCAN_EVERY//60} мин.")
    while True:
        try:
            symbols = get_liquid_symbols()
            if not symbols:
                log.warning("нет символов, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(symbols)} монет на коридоры...")
            found = 0; now = time.time()
            for sym in symbols:
                if sym in last_signal and now - last_signal[sym] < COOLDOWN: continue
                try:
                    m = get_klines(sym, "15", 60)
                    if not m: continue
                    r = detect_range(m)
                except Exception as e:
                    log.warning(f"{sym}: {e}"); continue
                if r:
                    log.info(f"✅ КОРИДОР {sym} ширина {r['width_pct']:.1f}% время {r['time_in_range_min']}мин")
                    send_tg(signal_text(sym, r)); last_signal[sym] = now; found += 1
                time.sleep(0.3)
            log.info(f"Скан завершён. Коридоров: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
