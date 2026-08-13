#!/usr/bin/env python3
"""
БОТ ЛОВЛИ НОЖА (разворот после резкого импульса) — ЛОНГ + ШОРТ
ЛОНГ:  резкое ПАДЕНИЕ >=DROP% -> зелёная разворотная свеча + объём -> лонг. Стоп под дно, тейк 1:3.
ШОРТ:  резкий ВЗЛЁТ  >=DROP% -> красная разворотная свеча + объём -> шорт. Стоп над пик, тейк 1:3.
Вход только на РАЗВОРОТЕ (не в летящий импульс). Карточка с % стопа и тейка.
НЕ торгует. Bybit через прокси + Телеграм.
"""
import os, time, logging, requests
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "20")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "80"))
ENABLE_LONG  = os.environ.get("ENABLE_LONG", "1") == "1"
ENABLE_SHORT = os.environ.get("ENABLE_SHORT", "1") == "1"
DROP_WIN    = int(os.environ.get("DROP_WIN", "5"))          # окно импульса (свечей 15м)
DROP_PCT    = float(os.environ.get("DROP_PCT", "7"))        # мин. импульс %
MAX_BARS_AFTER = int(os.environ.get("MAX_BARS_AFTER", "2")) # дно/пик было не дальше N свечей
MIN_BOUNCE  = float(os.environ.get("MIN_BOUNCE", "0.5"))    # мин. отскок от дна/пика %
MAX_BOUNCE  = float(os.environ.get("MAX_BOUNCE", "4"))      # макс. отскок (иначе поздно)
VOL_MULT    = float(os.environ.get("VOL_MULT", "1.5"))      # объём на развороте
SL_BUFFER   = float(os.environ.get("SL_BUFFER", "0.5"))    # ATR-буфер за дно/пик
RETRACE_CAP = float(os.environ.get("RETRACE_CAP", "0.7"))  # потолок тейка = X% отката импульса
SCAN_EVERY  = int(os.environ.get("SCAN_EVERY", "300"))
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

def get_symbols():
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

def atr(c, p=14):
    if len(c) < p+1: return None
    trs=[max(c[i]['h']-c[i]['l'],abs(c[i]['h']-c[i-1]['c']),abs(c[i]['l']-c[i-1]['c'])) for i in range(1,len(c))]
    return sum(trs[-p:])/p

def sma_vol(c, p): return sum(x['v'] for x in c[-p:])/p if len(c) >= p else None

def check_knife_long(m):
    """Нож вниз -> отскок вверх (лонг)."""
    if len(m) < DROP_WIN + 25: return None
    price = m[-1]['c']
    look = m[-(DROP_WIN+3):]
    peak = max(c['h'] for c in look)
    bottom = min(c['l'] for c in look)
    drop = (peak - bottom)/peak*100 if peak else 0
    if drop < DROP_PCT: return None
    bi = max(i for i,c in enumerate(look) if c['l']==bottom)
    if len(look)-1-bi > MAX_BARS_AFTER: return None
    if m[-1]['c'] <= m[-1]['o']: return None          # зелёная разворотная
    bounce = (price-bottom)/bottom*100 if bottom else 0
    if bounce < MIN_BOUNCE or bounce > MAX_BOUNCE: return None
    vma = sma_vol(m[:-1],20)
    if not vma or m[-1]['v'] < vma*VOL_MULT: return None
    a = atr(m,14)
    sl = bottom - (a*SL_BUFFER if a else bottom*0.002)
    risk = price - sl
    if risk <= 0: return None
    tp = price + risk*3
    # с учётом отката: тейк не должен быть выше реалистичной зоны отскока.
    # потолок = 70% отката импульса (пик - 70% пути от дна к пику зоны отскока)
    take_cap = bottom + (peak - bottom) * RETRACE_CAP  # напр. 0.7 = 70% импульса
    tp_src = "1:3 от стопа"
    if tp > take_cap:
        tp = take_cap
        tp_src = f"обрезан до {int(RETRACE_CAP*100)}% отката импульса"
    return {"side":"long","entry":price,"sl":sl,"tp":tp,"extreme":bottom,"peak":peak,
            "impulse":drop,"bounce":bounce,"vol_x":m[-1]['v']/vma,"risk_pct":risk/price*100,
            "reward_pct":(tp-price)/price*100,"tp_src":tp_src}

def check_knife_short(m):
    """Памп вверх -> откат вниз (шорт)."""
    if len(m) < DROP_WIN + 25: return None
    price = m[-1]['c']
    look = m[-(DROP_WIN+3):]
    peak = max(c['h'] for c in look)
    trough = min(c['l'] for c in look)
    rise = (peak - trough)/trough*100 if trough else 0
    if rise < DROP_PCT: return None
    pi = max(i for i,c in enumerate(look) if c['h']==peak)
    if len(look)-1-pi > MAX_BARS_AFTER: return None
    if m[-1]['c'] >= m[-1]['o']: return None          # красная разворотная
    drop_from_peak = (peak-price)/peak*100 if peak else 0
    if drop_from_peak < MIN_BOUNCE or drop_from_peak > MAX_BOUNCE: return None
    vma = sma_vol(m[:-1],20)
    if not vma or m[-1]['v'] < vma*VOL_MULT: return None
    a = atr(m,14)
    sl = peak + (a*SL_BUFFER if a else peak*0.002)
    risk = sl - price
    if risk <= 0: return None
    tp = price - risk*3
    # с учётом отката: тейк не ниже реалистичной зоны отката вниз (70% импульса)
    take_cap = peak - (peak - trough) * RETRACE_CAP
    tp_src = "1:3 от стопа"
    if tp < take_cap:
        tp = take_cap
        tp_src = f"обрезан до {int(RETRACE_CAP*100)}% отката импульса"
    return {"side":"short","entry":price,"sl":sl,"tp":tp,"extreme":peak,"trough":trough,
            "impulse":rise,"bounce":drop_from_peak,"vol_x":m[-1]['v']/vma,"risk_pct":risk/price*100,
            "reward_pct":(price-tp)/price*100,"tp_src":tp_src}

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def build_card(sym, s):
    name = sym.replace("USDT","")
    risk_pct = s['risk_pct']
    reward_pct = s['reward_pct']
    rr = reward_pct/risk_pct if risk_pct else 0
    if s['side'] == "long":
        head = f"🟢 <b>ЛОНГ (ловля ножа): {name}/USDT</b>"
        what = (f"Резкое ПАДЕНИЕ -{s['impulse']:.1f}% (нож с {fp(s['peak'])} до дна {fp(s['extreme'])}). "
                f"Цена оттолкнулась от дна: зелёная свеча +{s['bounce']:.1f}% с объёмом ×{s['vol_x']:.1f} "
                f"(покупатель пришёл). Вход на отскоке вверх.")
        stop_line = f"🛑 <b>Stop:</b> {fp(s['sl'])}  под дно ножа"
        take_line = f"✅ <b>Take:</b> {fp(s['tp'])}  вверх ({s['tp_src']})"
    else:
        head = f"🔴 <b>ШОРТ (ловля пампа): {name}/USDT</b>"
        what = (f"Резкий ВЗЛЁТ +{s['impulse']:.1f}% (памп с {fp(s['trough'])} до пика {fp(s['extreme'])}). "
                f"Цена откатила от пика: красная свеча -{s['bounce']:.1f}% с объёмом ×{s['vol_x']:.1f} "
                f"(продавец пришёл). Вход на откате вниз.")
        stop_line = f"🛑 <b>Stop:</b> {fp(s['sl'])}  над пик пампа"
        take_line = f"✅ <b>Take:</b> {fp(s['tp'])}  вниз ({s['tp_src']})"
    return (f"{head}\n\n"
            f"💰 <b>Вход:</b> {fp(s['entry'])}\n"
            f"{stop_line}\n"
            f"{take_line}\n\n"
            f"📉 <b>СТОП-ЛОСС: -{risk_pct:.2f}%</b>\n"
            f"📈 <b>ТЕЙК-ПРОФИТ: +{reward_pct:.2f}%</b>\n"
            f"⚖️ Риск/прибыль: 1:{rr:.1f}\n\n"
            f"📋 {what}\n\n"
            f"⚠️ Ловля ножа/пампа — опасно: импульс может продолжиться. "
            f"Стоп за экстремум обязателен, не двигай. Без большого плеча.\n"
            f"📊 https://www.bybit.com/trade/usdt/{sym}?interval=15")

def run():
    log.info("🤖 Бот ловли ножа (лонг+шорт, вход на развороте) запущен")
    log.info(f"Топ{TOP_N} | импульс>={DROP_PCT}% за {DROP_WIN}св | Лонг={ENABLE_LONG} Шорт={ENABLE_SHORT} | объём×{VOL_MULT}")
    send_tg(f"🤖 <b>Бот ловли ножа запущен</b>\n🟢 Резкое падение → отскок → лонг\n🔴 Резкий памп → откат → шорт\nВход только на РАЗВОРОТЕ. Стоп/тейк в % в каждой карточке. 1:3.")
    while True:
        try:
            symbols = get_symbols()
            if not symbols:
                log.warning("нет символов, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(symbols)} монет на ножи/пампы...")
            found = 0; now = time.time()
            for sym in symbols:
                try:
                    m = get_klines(sym, "15", 60)
                    if not m: continue
                    sig = None
                    if ENABLE_LONG:  sig = check_knife_long(m)
                    if not sig and ENABLE_SHORT: sig = check_knife_short(m)
                    if not sig: continue
                    key = f"{sym}_{sig['side']}"
                    if key in last_signal and now - last_signal[key] < COOLDOWN: continue
                except Exception as e:
                    log.warning(f"{sym}: {e}"); continue
                log.info(f"✅ {sig['side'].upper()} {sym} импульс {sig['impulse']:.1f}%")
                send_tg(build_card(sym, sig)); last_signal[key] = now; found += 1
                time.sleep(0.3)
            log.info(f"Скан завершён. Сигналов: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
