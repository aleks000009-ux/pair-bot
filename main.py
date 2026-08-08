#!/usr/bin/env python3
"""
СИГНАЛЬНЫЙ БОТ — Отскок от уровня (ЛОНГ + ШОРТ)

ЛОНГ (топ роста):  импульс вверх -> коррекция вниз -> отскок от ЛОК. МИНИМУМА вверх (зелёная свеча)
                   SL под минимум, TP вверх 1:3
ШОРТ (топ падения): импульс вниз -> коррекция вверх -> отскок от ЛОК. МАКСИМУМА вниз (красная свеча)
                   SL над максимум, TP вниз 1:3

Фон: |движение за 24ч| >= MIN_24H_PCT + направление дня совпадает.
НЕ торгует. Bybit-данные через прокси + сигнал в Телеграм.
"""
import os, time, logging, requests
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "10")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "40"))
MIN_24H_PCT = float(os.environ.get("MIN_24H_PCT", "25"))
ENABLE_LONG  = os.environ.get("ENABLE_LONG", "1") == "1"
ENABLE_SHORT = os.environ.get("ENABLE_SHORT", "1") == "1"
IMPULSE_WIN = int(os.environ.get("IMPULSE_WIN", "12"))
IMPULSE_PCT = float(os.environ.get("IMPULSE_PCT", "7"))
CORR_WIN    = int(os.environ.get("CORR_WIN", "10"))
CORR_MIN    = float(os.environ.get("CORR_MIN", "2"))
CORR_MAX    = float(os.environ.get("CORR_MAX", "8"))
MIN_BOUNCE_BARS = int(os.environ.get("MIN_BOUNCE_BARS", "1"))
MAX_BOUNCE_BARS = int(os.environ.get("MAX_BOUNCE_BARS", "4"))
MIN_BOUNCE_PCT  = float(os.environ.get("MIN_BOUNCE_PCT", "0.5"))
VOL_MULT    = float(os.environ.get("VOL_MULT", "1.5"))
SL_BUFFER   = float(os.environ.get("SL_BUFFER", "0.5"))
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

def get_movers():
    """Возвращает (gainers, losers) — топ роста и топ падения."""
    d = api_get("/tickers?category=linear")
    if not d or d.get("retCode") != 0: return [], []
    rows = []
    for t in d["result"]["list"]:
        if not t["symbol"].endswith("USDT"): continue
        try:
            turn = float(t.get("turnover24h") or 0); pct = float(t.get("price24hPcnt") or 0)*100
            if turn < MIN_VOL_USD: continue
            rows.append((t["symbol"], pct, turn))
        except (TypeError, ValueError): continue
    gainers = sorted([r for r in rows if r[1] >= MIN_24H_PCT], key=lambda x: x[1], reverse=True)[:TOP_N]
    losers  = sorted([r for r in rows if r[1] <= -MIN_24H_PCT], key=lambda x: x[1])[:TOP_N]
    return gainers, losers

def atr(c, p=14):
    if len(c) < p+1: return None
    trs=[max(c[i]['h']-c[i]['l'],abs(c[i]['h']-c[i-1]['c']),abs(c[i]['l']-c[i-1]['c'])) for i in range(1,len(c))]
    return sum(trs[-p:])/p

def sma_vol(c, p): return sum(x['v'] for x in c[-p:])/p if len(c) >= p else None

def check_long(symbol, pct24):
    day = get_klines(symbol, "D", 3)
    if not day or day[-1]['c'] <= day[-1]['o']: return None  # день зелёный
    m = get_klines(symbol, "15", 80)
    if not m or len(m) < 45: return None
    price = m[-1]['c']
    corr_zone = m[-CORR_WIN:]
    local_low = min(c['l'] for c in corr_zone)
    low_idx = max(i for i, c in enumerate(corr_zone) if c['l'] == local_low)
    bars = len(corr_zone) - 1 - low_idx
    if bars < MIN_BOUNCE_BARS or bars > MAX_BOUNCE_BARS: return None
    imp_zone = m[-CORR_WIN-IMPULSE_WIN:-CORR_WIN]
    imp_low = min(c['l'] for c in imp_zone); imp_high = max(c['h'] for c in imp_zone)
    impulse = (imp_high - imp_low)/imp_low*100 if imp_low else 0
    if impulse < IMPULSE_PCT: return None
    bounce = (price - local_low)/local_low*100 if local_low else 0
    if bounce < MIN_BOUNCE_PCT: return None
    if m[-1]['c'] <= m[-1]['o']: return None  # зелёная свеча (разворот вверх)
    depth = (imp_high - local_low)/imp_high*100 if imp_high else 0
    if not (CORR_MIN <= depth <= CORR_MAX): return None
    vma = sma_vol(m[:-1], 20)
    if not vma or m[-1]['v'] < vma * VOL_MULT: return None
    a = atr(m, 14)
    if not a: return None
    entry = price; sl = local_low - a*SL_BUFFER; risk = entry - sl
    if risk <= 0: return None
    return {"symbol":symbol,"side":"long","entry":entry,"sl":sl,"tp":entry+risk*3,
            "risk_pct":risk/entry*100,"impulse":impulse,"level":local_low,"bounce":bounce,
            "depth":depth,"bars":bars,"vol_x":m[-1]['v']/vma,"pct24":pct24}

def check_short(symbol, pct24):
    day = get_klines(symbol, "D", 3)
    if not day or day[-1]['c'] >= day[-1]['o']: return None  # день красный
    m = get_klines(symbol, "15", 80)
    if not m or len(m) < 45: return None
    price = m[-1]['c']
    corr_zone = m[-CORR_WIN:]
    local_high = max(c['h'] for c in corr_zone)
    high_idx = max(i for i, c in enumerate(corr_zone) if c['h'] == local_high)
    bars = len(corr_zone) - 1 - high_idx
    if bars < MIN_BOUNCE_BARS or bars > MAX_BOUNCE_BARS: return None
    imp_zone = m[-CORR_WIN-IMPULSE_WIN:-CORR_WIN]
    imp_low = min(c['l'] for c in imp_zone); imp_high = max(c['h'] for c in imp_zone)
    impulse = (imp_high - imp_low)/imp_high*100 if imp_high else 0  # падение
    if impulse < IMPULSE_PCT: return None
    drop = (local_high - price)/local_high*100 if local_high else 0
    if drop < MIN_BOUNCE_PCT: return None
    if m[-1]['c'] >= m[-1]['o']: return None  # красная свеча (разворот вниз)
    depth = (local_high - imp_low)/imp_low*100 if imp_low else 0
    if not (CORR_MIN <= depth <= CORR_MAX): return None
    vma = sma_vol(m[:-1], 20)
    if not vma or m[-1]['v'] < vma * VOL_MULT: return None
    a = atr(m, 14)
    if not a: return None
    entry = price; sl = local_high + a*SL_BUFFER; risk = sl - entry
    if risk <= 0: return None
    return {"symbol":symbol,"side":"short","entry":entry,"sl":sl,"tp":entry-risk*3,
            "risk_pct":risk/entry*100,"impulse":impulse,"level":local_high,"bounce":drop,
            "depth":depth,"bars":bars,"vol_x":m[-1]['v']/vma,"pct24":pct24}

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def signal_text(s):
    sym = s["symbol"].replace("USDT","")
    if s["side"] == "long":
        return (f"🟢 <b>ЛОНГ: {sym}/USDT</b>  (отскок от поддержки)\n\n"
                f"🔥 За 24ч: +{s['pct24']:.1f}%  (топ роста)\n"
                f"📈 Импульс вверх: +{s['impulse']:.1f}%\n"
                f"↩️ Коррекция вниз: -{s['depth']:.1f}% к минимуму {fp(s['level'])}\n"
                f"🟢 Отскок вверх: +{s['bounce']:.1f}%  ({s['bars']} св. назад дно)\n"
                f"📊 Объём: ×{s['vol_x']:.1f}\n\n"
                f"💰 <b>Вход:</b> {fp(s['entry'])}\n"
                f"🛑 <b>Stop:</b> {fp(s['sl'])}  под минимум (риск {s['risk_pct']:.2f}%)\n"
                f"✅ <b>Take 1:3:</b> {fp(s['tp'])}  (+{s['risk_pct']*3:.2f}%)\n\n"
                f"⚠️ Цена оттолкнулась от поддержки вверх. Подтверди разворот. Стоп/тейк не двигай.\n"
                f"📊 https://www.bybit.com/trade/usdt/{s['symbol']}?interval=15")
    else:
        return (f"🔴 <b>ШОРТ: {sym}/USDT</b>  (отскок от сопротивления)\n\n"
                f"🔥 За 24ч: {s['pct24']:.1f}%  (топ падения)\n"
                f"📉 Импульс вниз: -{s['impulse']:.1f}%\n"
                f"↪️ Коррекция вверх: +{s['depth']:.1f}% к максимуму {fp(s['level'])}\n"
                f"🔴 Отскок вниз: -{s['bounce']:.1f}%  ({s['bars']} св. назад пик)\n"
                f"📊 Объём: ×{s['vol_x']:.1f}\n\n"
                f"💰 <b>Вход:</b> {fp(s['entry'])}\n"
                f"🛑 <b>Stop:</b> {fp(s['sl'])}  над максимум (риск {s['risk_pct']:.2f}%)\n"
                f"✅ <b>Take 1:3:</b> {fp(s['tp'])}  ({s['risk_pct']*3:.2f}%)\n\n"
                f"⚠️ Цена оттолкнулась от сопротивления вниз. Подтверди разворот. Стоп/тейк не двигай.\n"
                f"📊 https://www.bybit.com/trade/usdt/{s['symbol']}?interval=15")

def run():
    log.info("🤖 Сигнальный бот (ЛОНГ+ШОРТ, отскок от уровня) запущен")
    log.info(f"Фон: топ{TOP_N} |24ч|>={MIN_24H_PCT}% | Лонг={ENABLE_LONG} Шорт={ENABLE_SHORT} | импульс>={IMPULSE_PCT}% коррекция{CORR_MIN}-{CORR_MAX}% объём×{VOL_MULT}")
    send_tg(f"🤖 <b>Сигнальный бот запущен</b>\n🟢 Топы роста → отскок от поддержки → лонг 1:3\n🔴 Топы падения → отскок от сопротивления → шорт 1:3\nСкан каждые {SCAN_EVERY//60} мин.")
    while True:
        try:
            gainers, losers = get_movers()
            log.info(f"Сканирую: {len(gainers)} растущих (лонг), {len(losers)} падающих (шорт)")
            found = 0; now = time.time()
            tasks = []
            if ENABLE_LONG:  tasks += [("long", g) for g in gainers]
            if ENABLE_SHORT: tasks += [("short", l) for l in losers]
            for side, (sym, pct, turn) in tasks:
                key = f"{sym}_{side}"
                if key in last_signal and now - last_signal[key] < COOLDOWN: continue
                try:
                    sig = check_long(sym, pct) if side == "long" else check_short(sym, pct)
                except Exception as e:
                    log.warning(f"{sym} {side}: {e}"); continue
                if sig:
                    log.info(f"✅ {side.upper()} {sym} (24ч {pct:+.1f}%)")
                    send_tg(signal_text(sig)); last_signal[key] = now; found += 1
                time.sleep(0.3)
            log.info(f"Скан завершён. Сигналов: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
