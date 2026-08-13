#!/usr/bin/env python3
"""
КАСКАДНЫЙ БОТ — пробой каскада уровней (мультитаймфрейм)
Уровни строятся на 4ч, пробой ловится на 15м. Логика пользователя.
Карточка-сигнал: направление, что пробиваем, стоп %, тейк % (next_level или 1:3), почему.
НЕ торгует. Bybit через прокси + Телеграм.
"""
import os, time, logging, requests
from typing import List, Dict, Tuple, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

API        = os.environ.get("API_PROXY", "https://bybit-proxy.aleks000009.workers.dev")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
CHAT_ID    = os.environ.get("CHAT_ID", "")
MIN_VOL_USD = float(os.environ.get("MIN_VOL_USD", "20")) * 1e6
TOP_N       = int(os.environ.get("TOP_N", "60"))
SCAN_EVERY  = int(os.environ.get("SCAN_EVERY", "600"))
COOLDOWN    = int(os.environ.get("COOLDOWN", "3600"))
USE_TREND_FILTER = os.environ.get("USE_TREND_FILTER", "1") == "1"

CASCADE_CONFIG = {
    "min_levels": int(os.environ.get("MIN_LEVELS", "3")),
    "max_levels": int(os.environ.get("MAX_LEVELS", "5")),
    "level_dist_min": float(os.environ.get("LEVEL_DIST_MIN", "0.003")),
    "level_dist_max": float(os.environ.get("LEVEL_DIST_MAX", "0.015")),
    "max_dist_from_price": float(os.environ.get("MAX_DIST_FROM_PRICE", "0.05")),
    "pressure_window": int(os.environ.get("PRESSURE_WINDOW", "8")),
    "pressure_min_bars": int(os.environ.get("PRESSURE_MIN_BARS", "4")),
    "vol_ma_lookback": int(os.environ.get("VOL_MA_LOOKBACK", "20")),
    "vol_threshold_mult": float(os.environ.get("VOL_THRESHOLD_MULT", "1.3")),
    "confirm_candles": int(os.environ.get("CONFIRM_CANDLES", "2")),
    "impulse_candles": int(os.environ.get("IMPULSE_CANDLES", "5")),
    "extrema_window": int(os.environ.get("EXTREMA_WINDOW", "3")),  # ИСПРАВЛЕНО: 3 вместо 5
}
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
    """Возвращает свечи в формате [ts,o,h,l,c,v] старые->новые (как ждёт логика)."""
    d = api_get(f"/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}")
    if not d or d.get("retCode") != 0 or not d.get("result", {}).get("list"): return None
    return [[int(c[0]),float(c[1]),float(c[2]),float(c[3]),float(c[4]),float(c[5])]
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

# ===== ЛОГИКА КАСКАДОВ (пользователя, окно исправлено на 3) =====
def build_cascades(candles, level_type, config):
    if len(candles) < 40: return []
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    W = config["extrema_window"]
    def find_extrema(prices, window=W):
        extrema = []
        for i in range(window, len(prices) - window):
            if prices[i] >= max(prices[i-window:i+window+1]):
                extrema.append((i, prices[i], 'resistance'))
            elif prices[i] <= min(prices[i-window:i+window+1]):
                extrema.append((i, prices[i], 'support'))
        return extrema
    extrema_list = find_extrema(highs) if level_type == 'resistance' else find_extrema(lows)
    # ФИКС: берём только экстремумы нужного типа (иначе support между resistance рвёт каскад)
    extrema_sorted = sorted([e for e in extrema_list if e[2] == level_type], key=lambda x: x[0])
    cascades = []
    for start_idx in range(len(extrema_sorted)):
        cascade = [extrema_sorted[start_idx]]
        for j in range(start_idx + 1, min(start_idx + config["max_levels"], len(extrema_sorted))):
            prev_idx, prev_price, _ = cascade[-1]
            curr_idx, curr_price, curr_type = extrema_sorted[j]
            if curr_type != level_type: break
            dist = abs(curr_price - prev_price) / prev_price
            if not (config["level_dist_min"] <= dist <= config["level_dist_max"]): break
            if level_type == 'resistance' and curr_price <= prev_price: break
            if level_type == 'support' and curr_price >= prev_price: break
            cascade.append(extrema_sorted[j])
        if len(cascade) >= config["min_levels"]:
            cascades.append(cascade)
    return cascades

def check_cascade_breakout_mtf(high_tf, low_tf, config, trend=None):
    if len(low_tf) < config["pressure_window"] + 5: return False, {}
    closes = [c[4] for c in low_tf]; highs = [c[2] for c in low_tf]
    lows = [c[3] for c in low_tf]; volumes = [c[5] for c in low_tf]
    last_close = closes[-1]; last_idx = len(closes) - 1
    res_c = build_cascades(high_tf, 'resistance', config)
    sup_c = build_cascades(high_tf, 'support', config)
    def sort_prox(cascades, cp):
        return sorted(cascades, key=lambda c: abs(cp - c[0][1]) / c[0][1])
    res_c = sort_prox(res_c, last_close); sup_c = sort_prox(sup_c, last_close)

    if trend != 'down':
        for cascade in res_c:
            l1_price = cascade[0][1]
            l2_price = cascade[1][1] if len(cascade) > 1 else None
            if abs(last_close - l1_price)/l1_price > config["max_dist_from_price"]: continue
            if last_close <= l1_price: continue
            bi = last_idx
            pl = lows[max(0, bi-config["pressure_window"]):bi]
            if len(pl) < config["pressure_min_bars"] or pl[-1] <= pl[0]: continue
            vs = volumes[max(0, bi-config["vol_ma_lookback"]):bi]
            if len(vs) < 10: continue
            if volumes[bi] < config["vol_threshold_mult"] * (sum(vs)/len(vs)): continue
            ce = min(len(closes), bi+config["confirm_candles"]+1)
            if any(c < l1_price for c in closes[bi+1:ce]): continue
            return True, {"direction":"LONG","levels":[l[1] for l in cascade],
                "break_level":l1_price,"next_level":l2_price,"stop_loss":l1_price*0.993,
                "entry_price":last_close}
    if trend != 'up':
        for cascade in sup_c:
            l1_price = cascade[0][1]
            l2_price = cascade[1][1] if len(cascade) > 1 else None
            if abs(last_close - l1_price)/l1_price > config["max_dist_from_price"]: continue
            if last_close >= l1_price: continue
            bi = last_idx
            ph = highs[max(0, bi-config["pressure_window"]):bi]
            if len(ph) < config["pressure_min_bars"] or ph[-1] >= ph[0]: continue
            vs = volumes[max(0, bi-config["vol_ma_lookback"]):bi]
            if len(vs) < 10: continue
            if volumes[bi] < config["vol_threshold_mult"] * (sum(vs)/len(vs)): continue
            ce = min(len(closes), bi+config["confirm_candles"]+1)
            if any(c > l1_price for c in closes[bi+1:ce]): continue
            return True, {"direction":"SHORT","levels":[l[1] for l in cascade],
                "break_level":l1_price,"next_level":l2_price,"stop_loss":l1_price*1.007,
                "entry_price":last_close}
    return False, {}

def get_trend_4h(candles):
    closes = [c[4] for c in candles]
    if len(closes) < 20: return None
    ma20 = sum(closes[-20:])/20
    if closes[-1] > ma20: return 'up'
    if closes[-1] < ma20: return 'down'
    return None

# ===== КАРТОЧКА =====
def fp(p):
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.5f}"
    return f"{p:.7f}"

def build_card(sym, info):
    s = sym.replace("USDT","")
    entry = info["entry_price"]; stop = info["stop_loss"]; brk = info["break_level"]
    nxt = info["next_level"]; direction = info["direction"]
    # тейк: строго 1:3 от стопа (сохраняет прибыльную математику).
    # next_level каскада обычно слишком близко (0.3-1.5%) и ломает R:R — не используем как цель.
    risk = abs(entry - stop)
    take = entry + risk*3 if direction=="LONG" else entry - risk*3
    take_src = "1:3 от стопа"
    # но если ПОСЛЕДНИЙ уровень каскада дальше тейка 1:3 — цель можно тянуть до него
    if info["levels"]:
        last_lvl = info["levels"][-1]
        if direction=="LONG" and last_lvl > take:
            take = last_lvl; take_src = "последний уровень каскада"
        elif direction=="SHORT" and last_lvl < take:
            take = last_lvl; take_src = "последний уровень каскада"
    risk_pct = risk/entry*100
    reward_pct = abs(take-entry)/entry*100
    rr = reward_pct/risk_pct if risk_pct else 0
    levels_str = " → ".join(fp(l) for l in info["levels"])
    if direction == "LONG":
        head = f"🟢 <b>ЛОНГ: {s}/USDT</b>  (пробой каскада вверх)"
        why = (f"Цена пробила первый уровень каскада сопротивлений {fp(brk)} "
               f"вверх, с объёмом и поджатием (минимумы росли перед пробоем). "
               f"Ожидание: импульс к следующим уровням каскада.")
    else:
        head = f"🔴 <b>ШОРТ: {s}/USDT</b>  (пробой каскада вниз)"
        why = (f"Цена пробила первый уровень каскада поддержек {fp(brk)} "
               f"вниз, с объёмом и поджатием (максимумы снижались перед пробоем). "
               f"Ожидание: импульс к следующим уровням каскада вниз.")
    return (f"{head}\n\n"
            f"🔨 Пробили уровень: {fp(brk)}\n"
            f"🪜 Каскад уровней: {levels_str}\n\n"
            f"💰 <b>Вход:</b> {fp(entry)}\n"
            f"🛑 <b>Stop:</b> {fp(stop)}  (риск {risk_pct:.2f}%)\n"
            f"✅ <b>Take:</b> {fp(take)}  (+{reward_pct:.2f}%, {take_src})\n"
            f"⚖️ Риск/прибыль ≈ 1:{rr:.1f}\n\n"
            f"📋 <b>Почему:</b> {why}\n\n"
            f"⚠️ Пробой мог быть ложным (шип за уровень → возврат). "
            f"Стоп поставлен за уровень пробоя — если цена вернулась за него, выходи. "
            f"Стоп/тейк не двигай.\n"
            f"📊 https://www.bybit.com/trade/usdt/{sym}?interval=15")

def run():
    log.info("🤖 Каскадный бот (пробой каскада уровней, 4ч→15м) запущен")
    log.info(f"Монеты топ{TOP_N} | уровни {CASCADE_CONFIG['min_levels']}-{CASCADE_CONFIG['max_levels']} "
             f"расст {CASCADE_CONFIG['level_dist_min']*100:.1f}-{CASCADE_CONFIG['level_dist_max']*100:.1f}% "
             f"окно {CASCADE_CONFIG['extrema_window']} | тренд-фильтр {USE_TREND_FILTER}")
    send_tg(f"🤖 <b>Каскадный бот запущен</b>\nУровни на 4ч → пробой на 15м → сигнал с карточкой.\nЖду пробои каскадов. Сигналов будет немного — каскады редки, это нормально.")
    while True:
        try:
            symbols = get_symbols()
            if not symbols:
                log.warning("нет символов, повтор 60с"); time.sleep(60); continue
            log.info(f"Сканирую {len(symbols)} монет на каскадные пробои...")
            found = 0; now = time.time()
            for sym in symbols:
                if sym in last_signal and now - last_signal[sym] < COOLDOWN: continue
                try:
                    h4 = get_klines(sym, "240", 100)
                    if not h4 or len(h4) < 40: continue
                    m15 = get_klines(sym, "15", 60)
                    if not m15 or len(m15) < 30: continue
                    trend = get_trend_4h(h4) if USE_TREND_FILTER else None
                    signal, info = check_cascade_breakout_mtf(h4, m15, CASCADE_CONFIG, trend)
                except Exception as e:
                    log.warning(f"{sym}: {e}"); continue
                if signal:
                    log.info(f"✅ {info['direction']} {sym} пробой {info['break_level']}")
                    send_tg(build_card(sym, info)); last_signal[sym] = now; found += 1
                time.sleep(0.4)
            log.info(f"Скан завершён. Сигналов: {found}")
        except Exception as e: log.error(f"цикл: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    run()
