#!/usr/bin/env python3
"""
БОТ АНОМАЛЬНЫХ СВЕЧЕЙ — вход ПО ТРЕНДУ против аномального выброса
Идея: тренд на дневке. Аномальная свеча ПРОТИВ тренда (в 2+ раза больше обычной) = выброс,
который тренд поглотит. Входим ПО тренду.
  Тренд ВНИЗ + аномальная ЗЕЛЁНАЯ свеча (выброс вверх) -> ШОРТ (тренд погасит)
  Тренд ВВЕРХ + аномальная КРАСНАЯ свеча (пролив вниз) -> ЛОНГ (тренд выкупит)
Аномалия = свеча >= ANOMALY_MULT × средней свечи монеты (на 15м) + объём.
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
ANOMALY_MULT= float(os.environ.get("ANOMALY_MULT", "2.0"))  # свеча >= X× средней
AVG_LOOKBACK= int(os.environ.get("AVG_LOOKBACK", "20"))      # за сколько свечей средняя
VOL_MULT    = float(os.environ.get("VOL_MULT", "1.5"))
SL_BUFFER   = float(os.environ.get("SL_BUFFER", "0.3"))
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

def get_trend_daily(daily):
    closes = [c['c'] for c in daily]
    if len(closes) < 20: return None
    ma = sum(closes[-20:])/20
    if closes[-1] > ma*1.01: return 'up'
    if closes[-1] < ma*0.99: return 'down'
    return None

def avg_candle_size(m, lb):
    zone = m[-lb-1:-1]
    return sum(c['h']-c['l'] for c in zone)/len(zone) if zone else None

def check_anomaly(m15, daily):
    trend = get_trend_daily(daily)
    if trend is None: return None
    if len(m15) < 30: return None
    last = m15[-1]
    avg = avg_candle_size(m15, AVG_LOOKBACK)
    if not avg or avg <= 0: return None
    ratio = (last['h']-last['l'])/avg
    if ratio < ANOMALY_MULT: return None
    vma = sma_vol(m15[:-1], 20)
    if not vma or last['v'] < vma*VOL_MULT: return None
    vol_x = last['v']/vma
    price = last['c']; a = atr(m15,14)
    is_green = last['c'] > last['o']; is_red = last['c'] < last['o']
    # тренд вниз + зелёный выброс -> ШОРТ
    if trend == 'down' and is_green:
        peak = last['h']; sl = peak + (a*SL_BUFFER if a else peak*0.002); risk = sl-price
        if risk <= 0: return None
        return {"side":"short","trend":trend,"entry":price,"sl":sl,"tp":price-risk*3,
                "extreme":peak,"ratio":ratio,"vol_x":vol_x,"risk_pct":risk/price*100,
                "candle":"зелёный выброс вверх"}
    # тренд вверх + красный пролив -> ЛОНГ
    if trend == 'up' and is_red:
        bottom = last['l']; sl = bottom - (a*SL_BUFFER if a else bottom*0.002); risk = price-sl
        if risk <= 0: return None
        return {"side":"long","trend":trend,"entry":price,"sl":sl,"tp":price+risk*3,
                "extreme":bottom,"ratio":ratio,"vol_x":vol_x,"risk_pct":risk/price*100,
                "candle":"красный пролив вниз"}
    return None

def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def build_card(sym, s):
    name = sym.replace("USDT","")
    risk_pct = s['risk_pct']; reward_pct = risk_pct*3
    if s['side'] == "long":
        head = f"🟢 <b>ЛОНГ по тренду: {name}/USDT</b>"
        what = (f"Дневной тренд ВВЕРХ 📈. Появилась аномальная КРАСНАЯ свеча "
                f"({s['ratio']:.1f}× больше обычной, объём ×{s['vol_x']:.1f}) — пролив вниз против тренда. "
                f"Это выброс (кто-то сбросил), тренд вверх его выкупит. Вход ЛОНГ по тренду.")
        stop_line = f"🛑 <b>Stop:</b> {fp(s['sl'])}  под аномальную свечу"
    else:
        head = f"🔴 <b>ШОРТ по тренду: {name}/USDT</b>"
        what = (f"Дневной тренд ВНИЗ 📉. Появилась аномальная ЗЕЛЁНАЯ свеча "
                f"({s['ratio']:.1f}× больше обычной, объём ×{s['vol_x']:.1f}) — выброс вверх против тренда. "
                f"Это аномалия (кто-то закупил), тренд вниз её погасит. Вход ШОРТ по тренду.")
        stop_line = f"🛑 <b>Stop:</b> {fp(s['sl'])}  над аномальную свечу"
    return (f"{head}\n\n"
            f"📊 Тренд дневки: {'ВВЕРХ 📈' if s['trend']=='up' else 'ВНИЗ 📉'}\n"
            f"🕯 Аномальная свеча: {s['ratio']:.1f}× ({s['candle']})\n\n"
            f"💰 <b>Вход:</b> {fp(s['entry'])}\n"
            f"{stop_line}\n"
            f"✅ <b>Take:</b> {fp(s['tp'])}\n\n"
            f"📉 <b>СТОП-ЛОСС: -{risk_pct:.2f}%</b>\n"
            f"📈 <b>ТЕЙК-ПРОФИТ: +{reward_pct:.2f}%</b>\n"
            f"⚖️ Риск/прибыль: 1:3\n\n"
            f"📋 {what}\n\n"
            f"⚠️ Вход ПО тренду против аномального выброса. Логика: тренд поглотит выброс. "
            f"Стоп за аномальную свечу, не двигай. Без большого плеча.\n"
            f"📊 https://www.bybit.com/trade/usdt/{sym}?interval=15")

def run():
    log.info("🤖 Бот аномальных свечей (вход по тренду против выброса) запущен")
    log.info(f"Топ{TOP_N} | аномалия>={ANOMALY_MULT}× | тренд дневка | объём×{VOL_MULT}")
    send_tg(f"🤖 <b>Бот аномальных свечей запущен</b>\n"
            f"📉 Тренд вниз + зелёный выброс → ШОРТ\n"
            f"📈 Тренд вверх + красный пролив → ЛОНГ\n"
            f"Свеча ≥{ANOMALY_MULT}× обычной + объём. Вход ПО тренду. 1:3.")
    while True:
        try:
            symbols = get_symbols()
            if not symbols:
                log.warning("нет символов, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(symbols)} монет на аномальные свечи...")
            found = 0; now = time.time()
            for sym in symbols:
                if sym in last_signal and now - last_signal[sym] < COOLDOWN: continue
                try:
                    daily = get_klines(sym, "D", 25)
                    if not daily or len(daily) < 20: continue
                    m15 = get_klines(sym, "15", 40)
                    if not m15 or len(m15) < 30: continue
                    sig = check_anomaly(m15, daily)
                except Exception as e:
                    log.warning(f"{sym}: {e}"); continue
                if sig:
                    log.info(f"✅ {sig['side'].upper()} {sym} свеча {sig['ratio']:.1f}× тренд {sig['trend']}")
                    send_tg(build_card(sym, sig)); last_signal[sym] = now; found += 1
                time.sleep(0.35)
            log.info(f"Скан завершён. Сигналов: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
