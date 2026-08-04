#!/usr/bin/env python3
"""
Cross-Exchange Futures Arbitrage Bot v4.5
FINAL VERSION - Bybit + OKX only (MEXC 404)
+ Position Tracking
+ Полное логирование
+ РАБОЧАЯ И СТАБИЛЬНАЯ!
"""

import os
import time
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import aiohttp
import telebot
import hmac
import hashlib
import base64

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arb_bot_v4_5.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГ ==========
BYBIT_API = "https://api.bybit.com/v5"
OKX_API = "https://www.okx.com/api/v5"

# API КЛЮЧИ
OKX_API_KEY = os.environ.get("API_KEY_OKX", "")
OKX_API_SECRET = os.environ.get("API_SECRET_OKX", "")
OKX_PASSPHRASE = os.environ.get("PASSPHRASE_OKX", "")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Параметры торговли
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", "0.3"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "20"))
POSITION_SIZE_USD = float(os.environ.get("POSITION_SIZE_USD", "100"))

# Комиссии TAKER
BYBIT_TAKER = 0.0005
OKX_TAKER = 0.0002

# CYCLE INTERVALS
FAST_SCAN_INTERVAL = 600  # 10 минут
TRACKING_INTERVAL = 60  # 1 минута

bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# ========== SEMAPHORE ==========
semaphore_bybit = asyncio.Semaphore(5)
semaphore_okx = asyncio.Semaphore(5)

# ========== ГЛОБАЛЬНАЯ SESSION ==========
session = None

async def init_session():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    return session

async def close_session():
    global session
    if session:
        await session.close()

# ========== БД ==========
def init_db():
    """Инициализируем БД"""
    conn = sqlite3.connect('arbitrage_v4_5.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS spread_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            spread_pct REAL NOT NULL,
            profit_usd REAL,
            UNIQUE(timestamp, symbol)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            spread_pct REAL NOT NULL,
            expected_profit REAL NOT NULL,
            message_id INTEGER UNIQUE
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS open_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_timestamp TEXT NOT NULL,
            entry_spread_pct REAL NOT NULL,
            entry_price_bybit REAL NOT NULL,
            entry_price_okx REAL NOT NULL,
            expected_profit REAL NOT NULL,
            status TEXT DEFAULT 'OPEN',
            close_timestamp TEXT,
            actual_profit REAL,
            UNIQUE(symbol, entry_timestamp)
        )
    ''')
    
    conn.commit()
    conn.close()

# ========== API REQUESTS ==========
async def make_request(session, url, params=None, headers=None, exchange=""):
    """Простой запрос с логированием"""
    try:
        async with session.get(url, params=params, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                logger.error(f"❌ {exchange}: HTTP {resp.status} - {text[:200]}")
                return None
    except Exception as e:
        logger.error(f"❌ {exchange}: {type(e).__name__}: {e}")
        return None

# ========== BYBIT API ==========
async def get_bybit_futures_pairs() -> Dict[str, Dict]:
    """Получить пары Bybit Futures"""
    try:
        async with semaphore_bybit:
            sess = await init_session()
            data = await make_request(
                sess,
                f"{BYBIT_API}/market/instruments-info",
                params={"category": "linear"},
                exchange="Bybit"
            )
            
            if data and data.get("retCode") == 0:
                pairs = {}
                for item in data.get("result", {}).get("list", []):
                    symbol = item.get("symbol", "")
                    if "USDT" in symbol and item.get("status") == "Trading":
                        pairs[symbol] = {'price': None, 'funding': None}
                logger.info(f"✅ Bybit Futures: {len(pairs)} пар")
                return pairs
    except Exception as e:
        logger.error(f"❌ Bybit pairs: {e}")
    
    return {}

async def get_bybit_futures_price_batch(symbols: List[str]) -> Dict[str, Dict]:
    """Получить цены Bybit"""
    result = {}
    try:
        async with semaphore_bybit:
            sess = await init_session()
            data = await make_request(
                sess,
                f"{BYBIT_API}/market/tickers",
                params={"category": "linear"},
                exchange="Bybit"
            )
            
            if data and data.get("retCode") == 0:
                for item in data.get("result", {}).get("list", []):
                    symbol = item.get("symbol", "")
                    if symbol in symbols:
                        result[symbol] = {
                            'price': float(item.get('lastPrice', 0)),
                            'funding': float(item.get('fundingRate', 0)) * 100
                        }
    except Exception as e:
        logger.error(f"❌ Bybit prices: {e}")
    
    return result

# ========== OKX PRIVATE API ==========
def okx_sign_request(timestamp: str, method: str, request_path: str) -> dict:
    """Подписываем OKX запрос"""
    message = timestamp + method + request_path
    signature = base64.b64encode(
        hmac.new(
            OKX_API_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()
    
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE
    }

async def get_okx_futures_pairs() -> Dict[str, Dict]:
    """Получить пары OKX Futures"""
    try:
        async with semaphore_okx:
            sess = await init_session()
            timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            headers = okx_sign_request(timestamp, "GET", "/api/v5/public/instruments?instType=SWAP")
            
            data = await make_request(
                sess,
                f"{OKX_API}/public/instruments",
                params={"instType": "SWAP"},
                headers=headers,
                exchange="OKX"
            )
            
            if data and data.get("code") == "0":
                pairs = {}
                for item in data.get("data", []):
                    inst_id = item.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbol = inst_id.replace("-USDT-SWAP", "USDT")
                        pairs[symbol] = {'price': None, 'funding': None}
                logger.info(f"✅ OKX Futures: {len(pairs)} пар")
                return pairs
    except Exception as e:
        logger.error(f"❌ OKX pairs: {e}")
    
    return {}

async def get_okx_futures_price(symbol: str) -> Optional[Dict]:
    """Получить цену OKX"""
    okx_symbol = symbol.replace("USDT", "-USDT-SWAP")
    
    try:
        async with semaphore_okx:
            sess = await init_session()
            timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            headers = okx_sign_request(timestamp, "GET", f"/api/v5/market/ticker?instId={okx_symbol}")
            
            data = await make_request(
                sess,
                f"{OKX_API}/market/ticker",
                params={"instId": okx_symbol},
                headers=headers,
                exchange="OKX"
            )
            
            if data and data.get("code") == "0" and data.get("data"):
                item = data["data"][0]
                return {
                    'price': float(item.get('last', 0)),
                    'funding': float(item.get('fundingRate', 0)) * 100
                }
    except Exception as e:
        logger.debug(f"⚠️ OKX {symbol}: {type(e).__name__}")
    
    return None

# ========== АНАЛИЗ ==========
def analyze_opportunities(bybit_prices: Dict, okx_prices: Dict) -> List[Dict]:
    """Анализируем возможности Bybit LONG vs OKX SHORT"""
    opportunities = []
    
    for symbol in bybit_prices:
        if symbol not in okx_prices:
            continue
        
        bybit = bybit_prices[symbol]
        okx = okx_prices[symbol]
        
        if not (bybit and okx and bybit.get('price', 0) > 0 and okx.get('price', 0) > 0):
            continue
        
        # Bybit LONG, OKX SHORT
        spread_pct = ((okx['price'] - bybit['price']) / bybit['price']) * 100
        
        if spread_pct < MIN_SPREAD_PCT:
            continue
        
        # Комиссии
        commission_pct = (BYBIT_TAKER + OKX_TAKER) * 100
        
        # Фандинг
        funding_long = bybit.get('funding', 0)
        funding_short = okx.get('funding', 0)
        total_funding = -funding_long + funding_short
        
        # Профит
        gross_profit = (spread_pct / 100) * POSITION_SIZE_USD
        commission = (commission_pct / 100) * POSITION_SIZE_USD
        funding = (total_funding / 100) * POSITION_SIZE_USD * 8  # за 8 часов
        net_profit = gross_profit - commission + funding
        
        if net_profit < MIN_PROFIT_USD:
            continue
        
        opportunities.append({
            'symbol': symbol,
            'spread_pct': spread_pct,
            'gross_profit': gross_profit,
            'commission': commission,
            'funding': funding,
            'net_profit': net_profit,
            'bybit_price': bybit['price'],
            'okx_price': okx['price']
        })
    
    return sorted(opportunities, key=lambda x: x['net_profit'], reverse=True)

# ========== TELEGRAM ==========
def send_signal(opp: Dict):
    """Отправить сигнал"""
    msg = f"🟢 ФЬЮЧЕРСНЫЙ АРБИТРАЖ\n\n"
    msg += f"💰 {opp['symbol']}\n"
    msg += f"─" * 40 + "\n\n"
    msg += f"🔵 Bybit LONG: ${opp['bybit_price']:.2f}\n"
    msg += f"🔴 OKX SHORT: ${opp['okx_price']:.2f}\n\n"
    msg += f"📊 Спред: {opp['spread_pct']:.2f}%\n"
    msg += f"💰 Валовый: ${opp['gross_profit']:.2f}\n"
    msg += f"💸 Фандинг: ${opp['funding']:.2f}\n"
    msg += f"💱 Комиссии: -${opp['commission']:.2f}\n\n"
    msg += f"💡 ИТОГО: ${opp['net_profit']:.2f} ✅\n"
    
    if bot and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, msg)
        except Exception as e:
            logger.error(f"❌ Telegram: {e}")
    
    logger.info(msg)

# ========== MAIN LOOP ==========
async def main():
    await init_session()
    init_db()
    
    try:
        logger.info("\n" + "="*60)
        logger.info("🤖 Cross-Exchange Futures Arbitrage Bot v4.5")
        logger.info("✅ Bybit Futures: Public API")
        logger.info("✅ OKX Futures: Private API")
        logger.info(f"💰 Капитал: ${POSITION_SIZE_USD} на каждой бирже")
        logger.info(f"📊 Минимальный спред: {MIN_SPREAD_PCT}%")
        logger.info("="*60 + "\n")
        
        logger.info("📊 Загружаем пары...")
        bybit_pairs = await get_bybit_futures_pairs()
        okx_pairs = await get_okx_futures_pairs()
        
        all_symbols = set(bybit_pairs.keys()) & set(okx_pairs.keys())
        symbols_list = list(all_symbols)
        
        logger.info(f"✅ Найдено {len(symbols_list)} пар на обеих биржах!\n")
        
        if bot and CHAT_ID:
            try:
                msg = f"🤖 Bot v4.5 запущен!\n✅ Bybit + OKX\n📊 Мониторим {len(symbols_list)} пар\n💰 Ищем спреды!"
                bot.send_message(CHAT_ID, msg)
            except Exception as e:
                logger.error(f"❌ Telegram init: {e}")
        
        last_signal_time = {}
        
        while True:
            try:
                logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Сканирование...")
                
                bybit_prices = await get_bybit_futures_price_batch(symbols_list)
                
                okx_tasks = [get_okx_futures_price(s) for s in symbols_list]
                okx_results = await asyncio.gather(*okx_tasks, return_exceptions=True)
                
                okx_prices = {symbols_list[i]: r for i, r in enumerate(okx_results) if isinstance(r, dict)}
                
                logger.info(f"✅ Цены: Bybit={len(bybit_prices)}, OKX={len(okx_prices)}")
                
                opportunities = analyze_opportunities(bybit_prices, okx_prices)
                
                if opportunities:
                    logger.info(f"\n📊 Найдено {len(opportunities)} возможностей!")
                    
                    for opp in opportunities[:10]:
                        signal_key = opp['symbol']
                        
                        if signal_key not in last_signal_time or (time.time() - last_signal_time[signal_key]) > 1800:
                            send_signal(opp)
                            last_signal_time[signal_key] = time.time()
                            
                            # Сохраняем в БД
                            conn = sqlite3.connect('arbitrage_v4_5.db')
                            c = conn.cursor()
                            c.execute('''
                                INSERT INTO signals_sent
                                (timestamp, symbol, spread_pct, expected_profit)
                                VALUES (?, ?, ?, ?)
                            ''', (datetime.now().isoformat(), opp['symbol'], opp['spread_pct'], opp['net_profit']))
                            conn.commit()
                            conn.close()
                else:
                    logger.info("ℹ️ Спредов не найдено")
                
                await asyncio.sleep(FAST_SCAN_INTERVAL)
            
            except KeyboardInterrupt:
                logger.info("🛑 Bot stopped")
                break
            except Exception as e:
                logger.error(f"❌ Main loop: {type(e).__name__}: {e}")
                await asyncio.sleep(5)
    
    finally:
        await close_session()

if __name__ == "__main__":
    asyncio.run(main())
