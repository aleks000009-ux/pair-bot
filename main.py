#!/usr/bin/env python3
"""
СИГНАЛЬНЫЙ БОТ — Моментум-пуллбэк (логика Б: вход на 15м)
ФОН: топ роста 24ч + день зелёный. ВХОД на 15м: импульс -> откат -> всплеск объёма.
SL=ATR14(15m)*SL_MULT, TP=3*риск (1:3). НЕ торгует, только Bybit-данные + Телеграм.
"""
import os, time, logging, requests
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "10")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "40"))
MIN_24H_PCT = float(os.environ.get("MIN_24H_PCT", "10"))
IMPULSE_WIN = int(os.environ.get("IMPULSE_WIN", "12"))
IMPULSE_PCT = float(os.environ.get("IMPULSE_PCT", "5"))
PB_WIN      = int(os.environ.get("PB_WIN", "4"))
PB_MIN      = float(os.environ.get("PB_MIN", "1.5"))
PB_MAX      = float(os.environ.get("PB_MAX", "5"))
VOL_MULT    = float(os.environ.get("VOL_MULT", "1.5"))
SL_MULT     = float(os.environ.get("SL_MULT", "2.0"))
SCAN_EVERY  = int(os.environ.get("SCAN_EVERY", "300"))
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
            log.warning(f"HTTP {r.status_code}: {path[:60]}")
        except Exception as e: log.warning(f"fetch {i+1}/{retries}: {e}")
        time.sleep(2*(i+1))
    return None

def get_klines(symbol, interval, limit):
    d = api_get(f"/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}")
    if not d or d.get("retCode") != 0 or not d.get("result", {}).get("list"): return None
    return [{"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])}
            for c in reversed(d["result"]["list"])]

def get_gainers():
    d = api_get("/tickers?category=linear")
    if not d or d.get("retCode") != 0: return []
    rows = []
    for t in d["result"]["list"]:
        if not t["symbol"].endswith("USDT"): continue
        try:
            turn = float(t.get("turnover24h") or 0); pct = float(t.get("price24hPcnt") or 0)*100
            if turn < MIN_VOL_USD or pct < MIN_24H_PCT: continue
            rows.append((t["symbol"], pct, turn))
        except (TypeError, ValueError): continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:TOP_N]

def atr(c, p=14):
    if len(c) < p+1: return None
    trs=[max(c[i]['h']-c[i]['l'],abs(c[i]['h']-c[i-1]['c']),abs(c[i]['l']-c[i-1]['c'])) for i in range(1,len(c))]
    return sum(trs[-p:])/p

def sma_vol(c, p): return sum(x['v'] for x in c[-p:])/p if len(c) >= p else None

def check_signal(symbol, pct24):
    day = get_klines(symbol, "D", 3)
    if not day or day[-1]['c'] <= day[-1]['o']: return None
    m = get_klines(symbol, "15", 80)
    need = IMPULSE_WIN + PB_WIN + 21
    if not m or len(m) < need: return None
    price = m[-1]['c']
    win = m[-IMPULSE_WIN-PB_WIN:-PB_WIN]
    imp_low = min(c['l'] for c in win); imp_high = max(c['h'] for c in win)
    impulse = (imp_high - imp_low)/imp_low*100 if imp_low else 0
    if impulse < IMPULSE_PCT: return None
    recent_high = max(c['h'] for c in m[-PB_WIN-2:])
    pb = (recent_high - price)/recent_high*100 if recent_high else 0
    if not (PB_MIN <= pb <= PB_MAX): return None
    vma = sma_vol(m[:-1], 20)
    if not vma or m[-1]['v'] < vma * VOL_MULT: return None
    vol_x = m[-1]['v']/vma
    a = atr(m, 14)
    if not a: return None
    entry = price; sl = entry - a*SL_MULT; risk = entry - sl
    if risk <= 0: return None
    tp = entry + risk*3
    return {"symbol":symbol,"entry":entry,"sl":sl,"tp":tp,"risk_pct":risk/entry*100,
            "impulse":impulse,"pb":pb,"vol_x":vol_x,"pct24":pct24}

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def signal_text(s):
    sym = s["symbol"].replace("USDT","")
    return (f"🎯 <b>СИГНАЛ: {sym}/USDT</b>  (лонг)\n\n"
            f"🔥 За 24ч: +{s['pct24']:.1f}%  (топ роста)\n"
            f"📈 Импульс 15m: +{s['impulse']:.1f}%\n"
            f"↩️ Откат: -{s['pb']:.1f}% от пика\n"
            f"📊 Объём всплеск: ×{s['vol_x']:.1f}\n\n"
            f"💰 <b>Вход:</b> {fp(s['entry'])}\n"
            f"🛑 <b>Stop:</b> {fp(s['sl'])}  (риск {s['risk_pct']:.2f}%)\n"
            f"✅ <b>Take 1:3:</b> {fp(s['tp'])}  (+{s['risk_pct']*3:.2f}%)\n\n"
            f"⚠️ Подтверди вход: разворотная свеча 15m у уровня. Стоп/тейк не двигай.\n"
            f"📊 https://www.bybit.com/trade/usdt/{s['symbol']}?interval=15")

def run():
    log.info("🤖 Сигнальный бот (логика Б: вход на 15м) запущен")
    log.info(f"Фон: топ{TOP_N} рост24ч>={MIN_24H_PCT}% день-зелёный | Вход15m: импульс>={IMPULSE_PCT}% откат{PB_MIN}-{PB_MAX}% объём×{VOL_MULT} SL={SL_MULT}xATR")
    send_tg(f"🤖 <b>Сигнальный бот запущен</b>\nТопы роста → импульс+откат на 15m → лонг 1:3\nСкан каждые {SCAN_EVERY//60} мин. Жду сетапы...")
    while True:
        try:
            gainers = get_gainers()
            if not gainers:
                log.warning("нет гейнеров, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(gainers)} монет...")
            found = 0; now = time.time()
            for sym, pct, turn in gainers:
                if sym in last_signal and now - last_signal[sym] < COOLDOWN: continue
                try: sig = check_signal(sym, pct)
                except Exception as e: log.warning(f"{sym}: {e}"); continue
                if sig:
                    log.info(f"✅ СИГНАЛ {sym} (24ч +{pct:.1f}%)")
                    send_tg(signal_text(sig)); last_signal[sym] = now; found += 1
                time.sleep(0.3)
            log.info(f"Скан завершён. Сигналов: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
