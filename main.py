#!/usr/bin/env python3
"""
Bybit Arbitrage Signal Bot v1.0
Мониторит Bybit Spot vs Futures спреды
Отправляет сигналы ТОЛЬКО если спред покрывает комиссии
НЕ торгует, только проверяет возможности
"""

import os
import time
from datetime import datetime
import requests
import telebot

# ========== КОНФИГ ==========
BYBIT_KEY = os.environ.get("BYBIT_KEY_REAL", "")  # Bybit API key
BYBIT_SECRET = os.environ.get("BYBIT_SECRET_REAL", "")  # Bybit API secret
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Bybit API
BYBIT_API = "https://api.bybit.com/v5"

# Комиссии Bybit (в процентах)
MAKER_COMMISSION = 0.1  # 0.1%
TAKER_COMMISSION = 0.1  # 0.1%
TOTAL_COMMISSION = (MAKER_COMMISSION + TAKER_COMMISSION) * 2  # туда-сюда = 0.4%

# Минимальный спред для сигнала
MIN_SPREAD_PCT = TOTAL_COMMISSION + 0.1  # 0.5%

# Монеты которые есть И на Bybit Spot И на Bybit Futures (70+)
SYMBOLS = os.environ.get("SYMBOLS",
    "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOTUSDT,AVAXUSDT,LINKUSDT,MATICUSDT,"
    "LTCUSDT,TRXUSDT,FILUSDT,VETUSDT,ATOMUSDT,NEARUSDT,OPUSDT,ARBITUSDT,SUIUSDT,PEPEUSDT,"
    "ETHFIUSDT,LUNCUSDT,INUSDT,GALUSDT,JUPUSDT,THETAUSDT,MKRUSDT,SNXUSDT,MASKUSDT,LDOUSDT,"
    "CRVUSDT,ICPUSDT,SANUSDT,ALGOUSDT,UNIUSDT,GRTUSDT,SKLUSDT,KDOUSDT,CHZUSDT,MINAUSDT,"
    "TAOUSDT,ORDIUSDT,APTUSDT,QNTUSDT,WUSDT,INJUSDT,WOOUSDT,RNDRUSDT,TTKUSDT,ONDOUSDT,"
    "GMUSDT,BONKUSDT,VXUSDT,ARKUSDT,PYTHUSDT,ZECUSDT,DYDXUSDT,RAYUSDT,FLMUSDT,STRKUSDT,"
    "AIUSDT,TFUELUSDT,SEIUSDT,GMXUSDT,FLOKIUSDT,RONUSDT,YFIUSDT,CVXUSDT,PRIMEUSDT,HBARUSDT,"
    "SUSDT,JUPITERUSDT,HIGHUSDT,DCUSDT,BALACERUSDT,RSRUSDT,AVEEUSDT,NOUSDT,WAVESUSDT,"
    "ROSIUSDT,KEYUSDT,MBLUSDT"
).split(",")
SYMBOLS = [s.strip() for s in SYMBOLS if s.strip()]

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "30"))

bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# ========== API ==========
def get_spot_price(symbol: str) -> float or None:
    """Получить цену Spot на Bybit"""
    try:
        r = requests.get(
            f"{BYBIT_API}/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            return float(data["result"]["list"][0]["lastPrice"])
        return None
    except:
        return None

def get_futures_price(symbol: str) -> float or None:
    """Получить цену Futures на Bybit"""
    try:
        r = requests.get(
            f"{BYBIT_API}/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            return float(data["result"]["list"][0]["lastPrice"])
        return None
    except:
        return None

def get_spot_24h_volume(symbol: str) -> float or None:
    """Получить объём Spot за 24h"""
    try:
        r = requests.get(
            f"{BYBIT_API}/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=5
        )
        data = r.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            volume_usd = float(data["result"]["list"][0].get("turnover24h", 0))
            return volume_usd
        return None
    except:
        return None

# ========== РАСЧЁТЫ ==========
def calculate_spread(spot_price: float, futures_price: float) -> float:
    """Спред в % (futures vs spot)"""
    return ((futures_price - spot_price) / spot_price) * 100

def is_profitable(spread: float) -> bool:
    """Прибыльный ли спред"""
    return abs(spread) > MIN_SPREAD_PCT

# ========== МОНИТОРИНГ ==========
def scan_opportunities():
    """Сканируем все монеты на Bybit"""
    opportunities = []
    
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Сканируем {len(SYMBOLS)} монет на Bybit...")
    
    for symbol in SYMBOLS:
        try:
            spot = get_spot_price(symbol)
            futures = get_futures_price(symbol)
            
            if not spot or not futures:
                continue
            
            spread = calculate_spread(spot, futures)
            
            if is_profitable(spread):
                volume = get_spot_24h_volume(symbol)
                
                opportunities.append({
                    'symbol': symbol,
                    'spot': spot,
                    'futures': futures,
                    'spread': spread,
                    'volume_24h': volume,
                    'profitable': True
                })
        except:
            pass
        
        time.sleep(0.05)  # не перегружать API
    
    return sorted(opportunities, key=lambda x: abs(x['spread']), reverse=True)

# ========== СИГНАЛЫ ==========
def send_signal(opp: dict):
    """Отправить сигнал в Telegram"""
    symbol = opp['symbol']
    spread = opp['spread']
    profit_after_comm = spread - TOTAL_COMMISSION
    
    if spread > 0:
        action = "LONG SPOT, SHORT FUTURES"
        emoji = "📈"
    else:
        action = "SHORT SPOT, LONG FUTURES"
        emoji = "📉"
    
    msg = f"{emoji} BYBIT АРБИТРАЖ\n\n"
    msg += f"💰 {symbol}\n"
    msg += f"SPOT: ${opp['spot']:.8f}\n"
    msg += f"FUTURES: ${opp['futures']:.8f}\n\n"
    msg += f"📊 Спред: {spread:+.2f}%\n"
    msg += f"💸 Комиссии: -{TOTAL_COMMISSION:.2f}%\n"
    msg += f"🎯 Профит: {profit_after_comm:+.2f}%\n\n"
    msg += f"📌 Действие: {action}\n"
    if opp['volume_24h']:
        msg += f"📈 Объём 24h: ${opp['volume_24h']:,.0f}"
    
    if bot:
        try:
            bot.send_message(CHAT_ID, msg)
        except:
            pass
    
    print(f"✅ СИГНАЛ {symbol}: спред {spread:+.2f}% → профит {profit_after_comm:+.2f}%")

# ========== MAIN ==========
def main():
    print("🤖 Bybit Arbitrage Signal Bot v1.0")
    print(f"📊 Мониторим Bybit Spot vs Futures")
    print(f"💸 Комиссии: {TOTAL_COMMISSION:.2f}% (туда-сюда)")
    print(f"🎯 Минимальный спред для сигнала: {MIN_SPREAD_PCT:.2f}%")
    print(f"📍 Мониторим: {len(SYMBOLS)} монет")
    print(f"⏰ Интервал: {SCAN_INTERVAL_SEC}s\n")
    
    if bot:
        try:
            bot.send_message(CHAT_ID, 
                f"🤖 Bybit Arbitrage Signal Bot запущен!\n\n"
                f"📊 Мониторим Bybit Spot vs Futures\n"
                f"💸 Комиссии: {TOTAL_COMMISSION:.2f}%\n"
                f"🎯 Минимум для сигнала: {MIN_SPREAD_PCT:.2f}%"
            )
        except:
            pass
    
    last_signals = {}
    
    while True:
        try:
            opportunities = scan_opportunities()
            
            if opportunities:
                print(f"📈 Найдено {len(opportunities)} возможностей на Bybit:")
                for opp in opportunities[:5]:
                    print(f"   {opp['symbol']}: {opp['spread']:+.2f}%")
                
                for opp in opportunities:
                    symbol = opp['symbol']
                    
                    if symbol not in last_signals or (time.time() - last_signals[symbol]) > 300:
                        send_signal(opp)
                        last_signals[symbol] = time.time()
            else:
                print(f"❌ Нет прибыльных спредов на Bybit (нужно > {MIN_SPREAD_PCT:.2f}%)")
            
            time.sleep(SCAN_INTERVAL_SEC)
        
        except KeyboardInterrupt:
            print("\n🛑 Бот остановлен")
            break
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
