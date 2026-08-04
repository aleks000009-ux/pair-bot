#!/usr/bin/env python3
"""
Cross-Exchange Futures Arbitrage Bot v4.4
+ MEXC Futures API FIX
  - Правильная подпись: accessKey + timestamp + queryString
  - Правильные заголовки: ApiKey, Request-Time, Signature
  - Полное логирование ошибок
  - Проверка переменных окружения
"""

import os
import time
import json
import logging
import asyncio
import sqlite3
import math
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, List, Dict, Tuple
import aiohttp
import telebot
from telebot import types
from urllib.parse import urlencode

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arb_bot_v4_4.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГ ==========
BYBIT_API = "https://api.bybit.com/v5"
MEXC_API = "https://api.mexc.com"
OKX_API = "https://www.okx.com/api/v5"

# API КЛЮЧИ
MEXC_API_KEY = os.environ.get("API_KEY", "")
MEXC_API_SECRET = os.environ.get("API_SECRET", "")
OKX_API_KEY = os.environ.get("API_KEY_OKX", "")
OKX_API_SECRET = os.environ.get("API_SECRET_OKX", "")
OKX_PASSPHRASE = os.environ.get("PASSPHRASE_OKX", "")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Параметры торговли
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", "0.3"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "20"))
MAX_VOLATILITY_PCT = float(os.environ.get("MAX_VOLATILITY_PCT", "5.0"))
POSITION_SIZE_USD = float(os.environ.get("POSITION_SIZE_USD", "100"))
MIN_CONVERGENCE_RATE = float(os.environ.get("MIN_CONVERGENCE_RATE", "0.7"))

# Комиссии TAKER
BYBIT_TAKER = 0.0005
MEXC_TAKER = 0.0005
OKX_TAKER = 0.0002

# RETRY
REQUEST_TIMEOUT = 10
MAX_RETRIES = 5
RETRY_DELAY_BASE = 0.5

# CYCLE INTERVALS
FAST_SCAN_INTERVAL = 600
SLOW_SCAN_INTERVAL = 3600
TRACKING_INTERVAL = 60

bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# ========== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
def check_env_vars():
    """Проверяем что все необходимые переменные загружены"""
    logger.info("="*60)
    logger.info("ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ:")
    logger.info(f"✅ MEXC_API_KEY: {'ДА' if MEXC_API_KEY else '❌ НЕТ'}")
    logger.info(f"✅ MEXC_API_SECRET: {'ДА' if MEXC_API_SECRET else '❌ НЕТ'}")
    logger.info(f"✅ OKX_API_KEY: {'ДА' if OKX_API_KEY else '❌ НЕТ'}")
    logger.info(f"✅ OKX_API_SECRET: {'ДА' if OKX_API_SECRET else '❌ НЕТ'}")
    logger.info(f"✅ OKX_PASSPHRASE: {'ДА' if OKX_PASSPHRASE else '❌ НЕТ'}")
    logger.info(f"✅ BOT_TOKEN: {'ДА' if BOT_TOKEN else '❌ НЕТ'}")
    logger.info(f"✅ CHAT_ID: {'ДА' if CHAT_ID else '❌ НЕТ'}")
    logger.info("="*60)

# ========== SEMAPHORE ==========
semaphore_bybit = asyncio.Semaphore(5)
semaphore_mexc = asyncio.Semaphore(5)
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

# ========== БД SETUP ==========
def init_db():
    """Инициализируем БД"""
    conn = sqlite3.connect('arbitrage_v4_4.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS spread_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange1 TEXT NOT NULL,
            exchange2 TEXT NOT NULL,
            price1 REAL NOT NULL,
            price2 REAL NOT NULL,
            spread_pct REAL NOT NULL,
            funding1 REAL,
            funding2 REAL,
            convergence_time INTEGER,
            profit_usd REAL,
            UNIQUE(timestamp, symbol, exchange1, exchange2)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS statistics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT UNIQUE NOT NULL,
            convergence_rate REAL,
            avg_convergence_time INTEGER,
            min_spread REAL,
            max_spread REAL,
            avg_spread REAL,
            avg_funding REAL,
            signal_count INTEGER DEFAULT 0,
            successful_trades INTEGER DEFAULT 0,
            recommendation TEXT,
            last_update TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

# ========== MEXC PRIVATE API - ИСПРАВЛЕНО! ==========
def mexc_sign_request(query_string: str, secret: str, timestamp: str) -> dict:
    """
    ИСПРАВЛЕННАЯ подпись MEXC Futures
    Формула: accessKey + timestamp + queryString
    """
    if not secret or not MEXC_API_KEY:
        return {}
    
    # Строка для подписи: accessKey + timestamp + queryString
    sign_string = MEXC_API_KEY + timestamp + query_string
    
    # HMAC-SHA256
    signature = hmac.new(
        secret.encode(),
        sign_string.encode(),
        hashlib.sha256
    ).hexdigest()
    
    # Правильные заголовки для MEXC Futures
    return {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": timestamp,
        "Signature": signature,
        "Content-Type": "application/json"
    }

async def make_request_with_retry(session, url, params=None, exchange="", headers=None, method="GET"):
    """Запрос с retry и ПОЛНЫМ логированием ошибок"""
    for attempt in range(MAX_RETRIES):
        try:
            async with session.request(method, url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    # ✅ ИСПРАВЛЕНИЕ: Полное логирование ошибок!
                    error_text = await resp.text()
                    logger.error(f"❌ {exchange}: HTTP {resp.status}")
                    logger.error(f"   URL: {url}")
                    logger.error(f"   Params: {params}")
                    logger.error(f"   Response: {error_text[:500]}")  # Первые 500 символов
                    
                    if resp.status == 429:
                        wait_time = RETRY_DELAY_BASE * (2 ** attempt)
                        logger.warning(f"⏱️ {exchange}: rate limit, retry за {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    return None
        except asyncio.TimeoutError:
            logger.error(f"⏱️ {exchange}: timeout на попытке {attempt+1}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY_BASE * (2 ** attempt))
        except aiohttp.ClientError as e:
            logger.error(f"❌ {exchange}: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ {exchange}: неожиданная ошибка: {type(e).__name__}: {e}")
            return None
    
    logger.error(f"❌ {exchange}: не удалось после {MAX_RETRIES} попыток")
    return None

# ========== BYBIT API ==========
async def get_bybit_futures_pairs() -> Dict[str, Dict]:
    """Получить ВСЕ пары Futures с Bybit"""
    try:
        async with semaphore_bybit:
            sess = await init_session()
            data = await make_request_with_retry(
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
    """Получить цены ВСЕ пар за раз"""
    result = {}
    try:
        async with semaphore_bybit:
            sess = await init_session()
            data = await make_request_with_retry(
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

# ========== MEXC PRIVATE API - ИСПРАВЛЕНО! ==========
async def get_mexc_futures_pairs() -> Dict[str, Dict]:
    """Получить пары MEXC Futures с ПРАВИЛЬНОЙ подписью"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            
            timestamp = str(int(time.time() * 1000))  # Текущее время в миллисекундах
            query_string = ""  # Нет параметров для этого запроса
            
            # Правильная подпись
            headers = mexc_sign_request(query_string, MEXC_API_SECRET, timestamp)
            
            logger.info(f"📍 MEXC: отправляем запрос...")
            logger.info(f"   Timestamp: {timestamp}")
            logger.info(f"   ApiKey: {MEXC_API_KEY[:10]}...")
            logger.info(f"   Signature: {headers.get('Signature', 'N/A')[:20]}...")
            
            # Пробуем несколько эндпоинтов MEXC
            endpoints = [
                f"{MEXC_API}/api/v1/contract/symbols",
                f"{MEXC_API}/open/api/v2/market/symbols",
                f"{MEXC_API}/open/contract/symbols"
            ]
            
            for endpoint in endpoints:
                logger.info(f"🔍 Пробуем endpoint: {endpoint}")
                
                data = await make_request_with_retry(
                    sess,
                    endpoint,
                    headers=headers,
                    exchange=f"MEXC ({endpoint})"
                )
                
                if data:
                    pairs = {}
                    
                    # Может быть разный формат ответа
                    if isinstance(data, dict):
                        symbols_list = data.get("data", [])
                        if isinstance(symbols_list, list):
                            for item in symbols_list:
                                symbol = item.get("symbol", "")
                                if symbol and "USDT" in symbol:
                                    pairs[symbol] = {'price': None, 'funding': None}
                    
                    if pairs:
                        logger.info(f"✅ MEXC Futures ({endpoint}): {len(pairs)} пар")
                        return pairs
            
            logger.error(f"❌ MEXC: ни один эндпоинт не вернул пары")
    except Exception as e:
        logger.error(f"❌ MEXC pairs exception: {type(e).__name__}: {e}")
    
    return {}

async def get_mexc_futures_price(symbol: str) -> Optional[Dict]:
    """Получить цену пары на MEXC Futures"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            
            timestamp = str(int(time.time() * 1000))
            query_string = f"symbol={symbol}"
            
            headers = mexc_sign_request(query_string, MEXC_API_SECRET, timestamp)
            
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/api/v1/contract/detail",
                params={"symbol": symbol},
                headers=headers,
                exchange=f"MEXC-{symbol}"
            )
            
            if data and isinstance(data, dict):
                item = data.get("data", {})
                if item:
                    return {
                        'price': float(item.get('lastPrice', 0)) or float(item.get('last', 0)),
                        'funding': float(item.get('fundingRate', 0)) * 100
                    }
    except Exception as e:
        logger.debug(f"⚠️ MEXC {symbol}: {type(e).__name__}")
    
    return None

# ========== OKX PRIVATE API ==========
def okx_sign_request(timestamp: str, method: str, request_path: str, body: str = "") -> dict:
    """Подписываем OKX запрос"""
    if not OKX_API_SECRET:
        return {}
    
    message = timestamp + method + request_path + body
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
            
            data = await make_request_with_retry(
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
                logger.info(f"✅ OKX: {len(pairs)} пар")
                return pairs
    except Exception as e:
        logger.error(f"❌ OKX pairs: {e}")
    
    return {}

async def get_okx_futures_price(symbol: str) -> Optional[Dict]:
    """Получить цену пары на OKX"""
    okx_symbol = symbol.replace("USDT", "-USDT-SWAP")
    
    try:
        async with semaphore_okx:
            sess = await init_session()
            timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            headers = okx_sign_request(timestamp, "GET", f"/api/v5/market/ticker?instId={okx_symbol}")
            
            data = await make_request_with_retry(
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
def analyze_opportunities(bybit_prices: Dict, mexc_prices: Dict, okx_prices: Dict) -> List[Dict]:
    """Анализируем возможности"""
    opportunities = []
    symbols_list = list(set(bybit_prices.keys()) & set(mexc_prices.keys()) & set(okx_prices.keys()))
    
    for symbol in symbols_list:
        bybit = bybit_prices.get(symbol)
        mexc = mexc_prices.get(symbol)
        okx = okx_prices.get(symbol)
        
        if not (bybit and mexc and okx and bybit.get('price', 0) > 0):
            continue
        
        pairs = [
            ('MEXC', bybit, mexc),
            ('OKX', bybit, okx)
        ]
        
        for exchange_name, prices_long, prices_short in pairs:
            spread_pct = ((prices_short['price'] - prices_long['price']) / prices_long['price']) * 100
            
            if spread_pct < MIN_SPREAD_PCT:
                continue
            
            opportunity = {
                'symbol': symbol,
                'exchange_short': exchange_name,
                'spread_pct': spread_pct,
                'price_long': prices_long['price'],
                'price_short': prices_short['price']
            }
            
            opportunities.append(opportunity)
    
    return sorted(opportunities, key=lambda x: x['spread_pct'], reverse=True)

# ========== MAIN LOOP ==========
async def main():
    check_env_vars()  # Проверяем переменные при старте
    
    await init_session()
    init_db()
    
    try:
        logger.info("\n🤖 Cross-Exchange Futures Arbitrage Bot v4.4")
        logger.info("✅ MEXC Futures API: ИСПРАВЛЕНО (правильная подпись + логирование)")
        logger.info("✅ OKX Futures API: Private API")
        logger.info("✅ Bybit: Public API\n")
        
        logger.info("📊 Загружаем все пары Futures...")
        bybit_pairs = await get_bybit_futures_pairs()
        mexc_pairs = await get_mexc_futures_pairs()
        okx_pairs = await get_okx_futures_pairs()
        
        all_symbols = set(bybit_pairs.keys()) & set(mexc_pairs.keys()) & set(okx_pairs.keys())
        symbols_list = list(all_symbols)
        
        logger.info(f"✅ Найдено {len(symbols_list)} пар на всех трех биржах!\n")
        
        if bot and CHAT_ID:
            try:
                msg = f"🤖 Bot v4.4 MEXC API FIX запущен!\n✅ {len(symbols_list)} пар найдено"
                bot.send_message(CHAT_ID, msg)
            except Exception as e:
                logger.error(f"❌ Telegram: {e}")
        
        while True:
            try:
                logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] СКАНИРОВАНИЕ...")
                
                bybit_prices = await get_bybit_futures_price_batch(symbols_list)
                
                mexc_tasks = [get_mexc_futures_price(s) for s in symbols_list]
                okx_tasks = [get_okx_futures_price(s) for s in symbols_list]
                
                mexc_results = await asyncio.gather(*mexc_tasks, return_exceptions=True)
                okx_results = await asyncio.gather(*okx_tasks, return_exceptions=True)
                
                mexc_prices = {symbols_list[i]: r for i, r in enumerate(mexc_results) if isinstance(r, dict)}
                okx_prices = {symbols_list[i]: r for i, r in enumerate(okx_results) if isinstance(r, dict)}
                
                logger.info(f"✅ Цены: Bybit={len(bybit_prices)}, MEXC={len(mexc_prices)}, OKX={len(okx_prices)}")
                
                opportunities = analyze_opportunities(bybit_prices, mexc_prices, okx_prices)
                
                if opportunities:
                    logger.info(f"📊 Найдено {len(opportunities)} спредов!")
                    for opp in opportunities[:5]:
                        logger.info(f"  {opp['symbol']}: {opp['spread_pct']:.2f}%")
                
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
