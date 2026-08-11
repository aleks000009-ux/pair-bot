#!/usr/bin/env python3
"""
СКАНЕР КОНСОЛИДАЦИЙ (плотный прямоугольник / ЗК)
Находит монеты в НАСТОЯЩЕЙ консолидации: цена плотно зажата между двумя
горизонтальными границами, свечи мелкие, много касаний обеих границ.
Отсеивает качели, тренды, шипы. Проверяет спокойствие и на старшем ТФ (1ч).
Даёт: границы, ширину, время, скорость, где цена, оценку стороны пробоя.
НЕ торгует. Bybit через прокси + Телеграм.
"""
import os, time, logging, requests
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "5")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "0"))          # 0 = все
WINDOW      = int(os.environ.get("WINDOW", "24"))
MIN_WIDTH   = float(os.environ.get("MIN_WIDTH", "1.0"))
MAX_WIDTH   = float(os.environ.get("MAX_WIDTH", "5.0"))
MAX_CANDLE_FRAC  = float(os.environ.get("MAX_CANDLE_FRAC", "0.5"))   # свечи <= X доли ширины
MIN_INSIDE_FRAC  = float(os.environ.get("MIN_INSIDE_FRAC", "0.80")) # доля свечей внутри
MIN_TOUCHES = int(os.environ.get("MIN_TOUCHES", "2"))
MIN_TIME_MIN= int(os.environ.get("MIN_TIME_MIN", "90"))
CHECK_HTF   = os.environ.get("CHECK_HTF", "1") == "1"    # проверять старший ТФ (1ч)
SCAN_EVERY  = int(os.environ.get("SCAN_EVERY", "600"))
COOLDOWN    = int(os.environ.get("COOLDOWN", "3600"))
last_signal = {}

def send_tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("BOT_TOKEN/CHAT_ID не заданы — в лог")
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
    return [s for s,_ in (rows if TOP_N<=0 else rows[:TOP_N])]

def detect_consolidation(candles, window):
    N = window
    if len(candles) < N + 5: return None
    zone = candles[-N:]
    if max(c['h'] for c in zone) <= 0: return None
    # ГРАНИЦЫ через перцентили (устойчивы к шипам)
    highs = sorted(c['h'] for c in zone)
    lows  = sorted(c['l'] for c in zone)
    hi = highs[min(N-1, int(N*0.85))]
    lo = lows[max(0, int(N*0.15))]
    if hi <= lo: return None
    mid = (hi+lo)/2
    width_pct = (hi-lo)/mid*100
    if not (MIN_WIDTH <= width_pct <= MAX_WIDTH): return None
    band = hi-lo
    # 1. плотность: мелкие свечи
    avg_range = sum(c['h']-c['l'] for c in zone)/N
    if avg_range > band*MAX_CANDLE_FRAC: return None
    # 2. большинство внутри
    inside = sum(1 for c in zone if lo*0.999 <= c['c'] <= hi*1.001)
    if inside/N < MIN_INSIDE_FRAC: return None
    # 3. касания обеих границ
    tb = band*0.25
    touch_hi = sum(1 for c in zone if c['h'] >= hi-tb)
    touch_lo = sum(1 for c in zone if c['l'] <= lo+tb)
    if touch_hi < MIN_TOUCHES or touch_lo < MIN_TOUCHES: return None
    # 4. не тренд
    q = max(1, N//4)
    drift = abs(sum(c['c'] for c in zone[:q])/q - sum(c['c'] for c in zone[-q:])/q)/mid*100
    if drift > width_pct*0.5: return None
    # время в коридоре
    bars = N
    for i in range(len(candles)-N-1, -1, -1):
        if lo <= candles[i]['l'] and candles[i]['h'] <= hi: bars += 1
        else: break
    time_min = bars*15
    # скорость
    crossings = 0; prev = zone[0]['c'] > mid
    for c in zone[1:]:
        a = c['c'] > mid
        if a != prev: crossings += 1; prev = a
    speed_min = (N/crossings*15) if crossings else N*15
    price = zone[-1]['c']; pos = (price-lo)/(hi-lo)*100
    # оценка стороны пробоя
    breakout = estimate_breakout(zone, hi, lo, mid, pos)
    return {'high':hi,'low':lo,'mid':mid,'width_pct':width_pct,'inside':inside,'N':N,
            'touch_hi':touch_hi,'touch_lo':touch_lo,'time_min':time_min,'bars':bars,
            'speed_min':speed_min,'price':price,'pos':pos,
            'candle_frac':avg_range/band*100,'breakout':breakout}

def estimate_breakout(zone, hi, lo, mid, pos):
    """Оценка вероятной стороны пробоя. Возвращает (сторона, причины[])."""
    up = 0; down = 0; reasons = []
    # позиция цены
    if pos >= 70: up += 1; reasons.append("цена жмётся к верху")
    elif pos <= 30: down += 1; reasons.append("цена жмётся к низу")
    # наклон: растут ли минимумы / падают ли максимумы (в последней половине)
    half = len(zone)//2
    lows_first = min(c['l'] for c in zone[:half]); lows_last = min(c['l'] for c in zone[half:])
    highs_first = max(c['h'] for c in zone[:half]); highs_last = max(c['h'] for c in zone[half:])
    if lows_last > lows_first * 1.001: up += 1; reasons.append("минимумы растут")
    if highs_last < highs_first * 0.999: down += 1; reasons.append("максимумы снижаются")
    # объём в верхней/нижней половине
    vol_up = sum(c['v'] for c in zone if c['c'] > mid)
    vol_dn = sum(c['v'] for c in zone if c['c'] <= mid)
    if vol_up > vol_dn * 1.3: up += 1; reasons.append("объём выше в верхней части")
    elif vol_dn > vol_up * 1.3: down += 1; reasons.append("объём выше в нижней части")
    if up > down: return ("ВВЕРХ", reasons)
    if down > up: return ("ВНИЗ", reasons)
    return ("не ясно", reasons)

def htf_calm(symbol):
    """Проверка: на 1ч монета тоже спокойна (не дикие качели)."""
    h1 = get_klines(symbol, "60", 12)  # последние 12 часов
    if not h1 or len(h1) < 8: return True  # нет данных — не блокируем
    hi = max(c['h'] for c in h1); lo = min(c['l'] for c in h1)
    mid = (hi+lo)/2
    if mid <= 0: return True
    range_pct = (hi-lo)/mid*100
    # если за 12ч на 1ч разброс больше 15% — это волатильная качель, не спокойный боковик
    return range_pct <= 15

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def fmt_time(m):
    if m < 60: return f"{m}мин"
    h=m//60; mm=m%60
    return f"{h}ч {mm}мин" if mm else f"{h}ч"

def signal_text(sym, r):
    s = sym.replace("USDT","")
    if r['pos'] <= 25: hint = "🟢 Цена у НИЗА — зона покупки (лонг к верху)"
    elif r['pos'] >= 75: hint = "🔴 Цена у ВЕРХА — зона продажи (шорт к низу)"
    else: hint = "⚪ Цена в середине — жди подхода к границе"
    side, reasons = r['breakout']
    if side == "ВВЕРХ": bo = f"🎯 Вероятный пробой: <b>ВВЕРХ</b> (склонность)"
    elif side == "ВНИЗ": bo = f"🎯 Вероятный пробой: <b>ВНИЗ</b> (склонность)"
    else: bo = f"🎯 Сторона пробоя: пока не ясно"
    reasons_txt = ("\n" + "\n".join(f"  • {x}" for x in reasons)) if reasons else ""
    return (f"📊 <b>КОНСОЛИДАЦИЯ: {s}/USDT</b>\n\n"
            f"⬆️ Верх: {fp(r['high'])}\n"
            f"⬇️ Низ:  {fp(r['low'])}\n"
            f"📏 Ширина: {r['width_pct']:.2f}%  (свечи {r['candle_frac']:.0f}% коридора)\n"
            f"⏱ В коридоре: {fmt_time(r['time_min'])}\n"
            f"🔄 Проход между границами: ~{fmt_time(int(r['speed_min']))}\n"
            f"📍 Сейчас: {fp(r['price'])} ({r['pos']:.0f}% от низа)\n\n"
            f"{bo}{reasons_txt}\n\n"
            f"{hint}\n\n"
            f"⚠️ Прогноз пробоя — это склонность, НЕ гарантия. Часто бывает "
            f"ложный пробой (шип за границу, потом разворот). Надёжнее: жди "
            f"ЗАКРЫТИЯ свечи за границей + объём, а не входи на голом прогнозе. "
            f"Стоп за противоположную границу. Комиссии съедают часть хода.\n"
            f"📊 https://www.bybit.com/trade/usdt/{sym}?interval=15")

def run():
    log.info("🤖 Сканер консолидаций (плотный прямоугольник) запущен")
    log.info(f"Монеты: {'ВСЕ' if TOP_N<=0 else TOP_N} | ширина {MIN_WIDTH}-{MAX_WIDTH}% | свечи<={MAX_CANDLE_FRAC} | внутри>={MIN_INSIDE_FRAC} | HTF-фильтр={CHECK_HTF}")
    send_tg(f"🤖 <b>Сканер консолидаций запущен</b>\nИщу плотные боковики-прямоугольники (как на схеме ЗК).\nФильтрую качели, тренды, шипы. Проверяю спокойствие на 1ч.\nДаю границы, ширину, время, скорость + оценку стороны пробоя.")
    while True:
        try:
            symbols = get_liquid_symbols()
            if not symbols:
                log.warning("нет символов, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(symbols)} монет на консолидации...")
            found = 0; now = time.time()
            for sym in symbols:
                if sym in last_signal and now - last_signal[sym] < COOLDOWN: continue
                try:
                    m = get_klines(sym, "15", 60)
                    if not m: continue
                    r = detect_consolidation(m, WINDOW)
                    if not r: continue
                    if r['time_min'] < MIN_TIME_MIN: continue
                    if CHECK_HTF and not htf_calm(sym): continue  # старший ТФ проверка
                except Exception as e:
                    log.warning(f"{sym}: {e}"); continue
                log.info(f"✅ КОНСОЛИДАЦИЯ {sym} ш{r['width_pct']:.1f}% t{r['time_min']}м пробой:{r['breakout'][0]}")
                send_tg(signal_text(sym, r)); last_signal[sym] = now; found += 1
                time.sleep(0.3)
            log.info(f"Скан завершён. Консолидаций: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
