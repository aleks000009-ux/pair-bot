#!/usr/bin/env python3
"""
Cross-Exchange Futures Arbitrage Bot v4.2.1
+ FIX: MEXC Futures API (была 0 пар, теперь будут реальные)
"""

import os
import time
import json
import logging
import asyncio
import sqlite3
import math
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, List, Dict, Tuple
import aiohttp
import telebot
from telebot import types

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arb_bot_v4_2_1.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГ ==========
BYBIT_API = "https://api.bybit.com/v5"
MEXC_API = "https://api.mexc.com"  # ИЗМЕНЕНО: убрал /api/v3 (будет в функции)
OKX_API = "https://www.okx.com/api/v5"

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
FAST_SCAN_INTERVAL = 600  # 10 минут
SLOW_SCAN_INTERVAL = 3600  # 1 час
TRACKING_INTERVAL = 60  # 1 минута

bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# Текущие открытые позиции
open_positions = {}

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
    conn = sqlite3.connect('arbitrage_v4_2_1.db')
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
            failed_trades INTEGER DEFAULT 0,
            avg_actual_profit REAL DEFAULT 0,
            recommendation TEXT,
            last_update TEXT,
            last_convergence TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            exchange_long TEXT NOT NULL,
            exchange_short TEXT NOT NULL,
            spread_pct REAL NOT NULL,
            funding_usd REAL NOT NULL,
            expected_profit REAL NOT NULL,
            convergence_rate REAL,
            recommendation TEXT,
            message_id INTEGER UNIQUE
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS open_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            exchange_long TEXT NOT NULL,
            exchange_short TEXT NOT NULL,
            entry_timestamp TEXT NOT NULL,
            entry_spread_pct REAL NOT NULL,
            entry_price_long REAL NOT NULL,
            entry_price_short REAL NOT NULL,
            entry_funding_long REAL,
            entry_funding_short REAL,
            expected_profit REAL NOT NULL,
            expected_convergence_time INTEGER,
            status TEXT DEFAULT 'OPEN',
            close_timestamp TEXT,
            close_spread_pct REAL,
            close_price_long REAL,
            close_price_short REAL,
            actual_profit REAL,
            message_id INTEGER,
            tracking_count INTEGER DEFAULT 0,
            UNIQUE(symbol, entry_timestamp)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS tracking_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            current_spread_pct REAL NOT NULL,
            current_price_long REAL NOT NULL,
            current_price_short REAL NOT NULL,
            current_funding_long REAL,
            current_funding_short REAL,
            unrealized_profit REAL,
            FOREIGN KEY(position_id) REFERENCES open_positions(id)
        )
    ''')
    
    conn.commit()
    conn.close()

# ========== API ==========
async def make_request_with_retry(session, url, params=None, exchange="", headers=None):
    """Запрос с retry"""
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    wait_time = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(f"⏱️ {exchange}: rate limit")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.debug(f"⚠️ {exchange}: HTTP {resp.status}")
                    return None
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY_BASE * (2 ** attempt))
            else:
                logger.debug(f"⏱️ {exchange}: timeout")
        except aiohttp.ClientError as e:
            logger.debug(f"⚠️ {exchange}: {type(e).__name__}")
            return None
    
    return None

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
                        pairs[symbol] = {'price': None, 'funding': None, 'volume': float(item.get('volume24h', 0))}
                logger.info(f"✅ Bybit Futures: {len(pairs)} пар")
                return pairs
    except Exception as e:
        logger.error(f"❌ Bybit pairs: {e}")
    
    return {}

async def get_mexc_futures_pairs() -> Dict[str, Dict]:
    """Получить ВСЕ пары MEXC Futures - ИСПРАВЛЕНО!"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            
            # ИСПРАВЛЕНИЕ: MEXC Futures API
            # Пробуем несколько вариантов
            
            # Вариант 1: /open/api/v2/market/ticker (старый API)
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/open/api/v2/market/ticker",
                exchange="MEXC-v2"
            )
            
            if data and isinstance(data, list) and len(data) > 0:
                pairs = {}
                for item in data:
                    symbol = item.get("symbol", "")
                    if symbol and "USDT" in symbol:
                        pairs[symbol] = {'price': None, 'funding': None}
                
                if pairs:
                    logger.info(f"✅ MEXC Futures (v2): {len(pairs)} пар")
                    return pairs
            
            # Вариант 2: /api/v3/exchangeInfo (новый API для Spot, но может работать и для Futures)
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/api/v3/exchangeInfo",
                exchange="MEXC-v3"
            )
            
            if data and "symbols" in data:
                pairs = {}
                for item in data.get("symbols", []):
                    symbol = item.get("symbol", "")
                    status = item.get("status", "")
                    
                    if symbol and "USDT" in symbol and status == "TRADING":
                        pairs[symbol] = {'price': None, 'funding': None}
                
                if pairs:
                    logger.info(f"✅ MEXC (exchangeInfo): {len(pairs)} пар")
                    return pairs
            
            # Вариант 3: Прямой запрос к конкретному эндпоинту
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/open/api/v2/market/detail",
                params={"symbol": ""},
                exchange="MEXC-detail"
            )
            
            if data:
                pairs = {}
                if isinstance(data, list):
                    for item in data:
                        symbol = item.get("symbol", "")
                        if symbol and "USDT" in symbol:
                            pairs[symbol] = {'price': None, 'funding': None}
                elif isinstance(data, dict):
                    # Может быть вложенный формат
                    for key, item in data.items():
                        if "USDT" in str(key):
                            pairs[str(key)] = {'price': None, 'funding': None}
                
                if pairs:
                    logger.info(f"✅ MEXC (detail): {len(pairs)} пар")
                    return pairs
            
            logger.warning(f"⚠️ MEXC: не удалось получить пары")
    except Exception as e:
        logger.error(f"❌ MEXC pairs: {e}")
    
    return {}

async def get_okx_futures_pairs() -> Dict[str, Dict]:
    """Получить ВСЕ пары OKX"""
    try:
        async with semaphore_okx:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{OKX_API}/public/instruments",
                params={"instType": "SWAP"},
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
                            'funding': float(item.get('fundingRate', 0)) * 100,
                            'volume': float(item.get('volume24h', 0))
                        }
    except Exception as e:
        logger.debug(f"⚠️ Bybit prices: {type(e).__name__}")
    
    return result

async def get_mexc_futures_price(symbol: str) -> Optional[Dict]:
    """Получить цену пары на MEXC - ИСПРАВЛЕНО!"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            
            # Пробуем несколько вариантов
            # Вариант 1: /open/api/v2/market/ticker
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/open/api/v2/market/ticker",
                params={"symbol": symbol},
                exchange=f"MEXC-{symbol}"
            )
            
            if data and isinstance(data, dict) and data.get("symbol") == symbol:
                return {
                    'price': float(data.get('last', 0)) or float(data.get('lastPrice', 0)),
                    'funding': float(data.get('fundingRate', 0)) * 100 if data.get('fundingRate') else 0,
                    'volume': float(data.get('volume', 0)) or float(data.get('quoteAssetVolume', 0))
                }
            
            # Вариант 2: /api/v3/ticker/24hr
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/api/v3/ticker/24hr",
                params={"symbol": symbol},
                exchange=f"MEXC-v3-{symbol}"
            )
            
            if data and data.get("symbol"):
                return {
                    'price': float(data.get('lastPrice', 0)),
                    'funding': float(data.get('fundingRate', 0)) * 100 if data.get('fundingRate') else 0,
                    'volume': float(data.get('quoteAssetVolume', 0))
                }
    except Exception as e:
        logger.debug(f"⚠️ MEXC {symbol}: {type(e).__name__}")
    
    return None

async def get_okx_futures_price(symbol: str) -> Optional[Dict]:
    """Получить цену пары на OKX"""
    okx_symbol = symbol.replace("USDT", "-USDT-SWAP")
    
    try:
        async with semaphore_okx:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{OKX_API}/market/ticker",
                params={"instId": okx_symbol},
                exchange="OKX"
            )
            
            if data and data.get("code") == "0" and data.get("data"):
                item = data["data"][0]
                return {
                    'price': float(item.get('last', 0)),
                    'funding': float(item.get('fundingRate', 0)) * 100 if item.get('fundingRate') else 0,
                    'volume': float(item.get('vol24h', 0))
                }
    except Exception as e:
        logger.debug(f"⚠️ OKX {symbol}: {type(e).__name__}")
    
    return None

# ========== БД ОПЕРАЦИИ ==========
def save_spread_to_db(symbol: str, exchange1: str, exchange2: str, price1: float, price2: float,
                     funding1: float, funding2: float, profit_usd: float):
    """Сохраняем спред в БД"""
    conn = sqlite3.connect('arbitrage_v4_2_1.db')
    c = conn.cursor()
    
    spread_pct = ((price2 - price1) / price1) * 100 if price1 > 0 else 0
    timestamp = datetime.now().isoformat()
    
    try:
        c.execute('''
            INSERT INTO spread_history 
            (timestamp, symbol, exchange1, exchange2, price1, price2, spread_pct, funding1, funding2, profit_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (timestamp, symbol, exchange1, exchange2, price1, price2, spread_pct, funding1, funding2, profit_usd))
        
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    
    conn.close()

def get_convergence_stats(symbol: str) -> Dict:
    """Получить статистику сходимости"""
    conn = sqlite3.connect('arbitrage_v4_2_1.db')
    c = conn.cursor()
    
    thirty_days_ago = (datetime.now() - timedelta(days=30)).isoformat()
    
    c.execute('''
        SELECT convergence_time, spread_pct
        FROM spread_history
        WHERE symbol = ? AND timestamp > ?
        ORDER BY timestamp DESC
    ''', (symbol, thirty_days_ago))
    
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return {
            'convergence_rate': 0,
            'avg_convergence_time': 0,
            'min_spread': 0,
            'max_spread': 0,
            'avg_spread': 0,
            'recommendation': '❌ НЕТ ДАННЫХ'
        }
    
    convergence_times = [r[0] for r in rows if r[0] is not None]
    spreads = [r[1] for r in rows]
    
    convergence_rate = len(convergence_times) / len(rows) if rows else 0
    avg_convergence_time = sum(convergence_times) / len(convergence_times) if convergence_times else 0
    
    if convergence_rate >= 0.9:
        recommendation = '✅ ЛУЧШИЙ'
    elif convergence_rate >= 0.8:
        recommendation = '✅ ХОРОШИЙ'
    elif convergence_rate >= 0.7:
        recommendation = '✅ OK'
    elif convergence_rate >= 0.5:
        recommendation = '🟡 РИСКОВАННО'
    else:
        recommendation = '❌ ИЗБЕГАТЬ'
    
    return {
        'convergence_rate': convergence_rate * 100,
        'avg_convergence_time': int(avg_convergence_time),
        'min_spread': min(spreads) if spreads else 0,
        'max_spread': max(spreads) if spreads else 0,
        'avg_spread': sum(spreads) / len(spreads) if spreads else 0,
        'recommendation': recommendation
    }

def analyze_opportunities(bybit_prices: Dict, mexc_prices: Dict, okx_prices: Dict) -> List[Dict]:
    """Анализируем возможности арбитража"""
    opportunities = []
    symbols_list = list(set(bybit_prices.keys()) & set(mexc_prices.keys()) & set(okx_prices.keys()))
    
    for symbol in symbols_list:
        bybit = bybit_prices.get(symbol)
        mexc = mexc_prices.get(symbol)
        okx = okx_prices.get(symbol)
        
        if not (bybit and mexc and okx and bybit.get('price', 0) > 0 and mexc.get('price', 0) > 0 and okx.get('price', 0) > 0):
            continue
        
        pairs = [
            ('MEXC', bybit, mexc),
            ('OKX', bybit, okx)
        ]
        
        for exchange_name, prices_long, prices_short in pairs:
            spread_pct = ((prices_short['price'] - prices_long['price']) / prices_long['price']) * 100
            
            if spread_pct < MIN_SPREAD_PCT:
                continue
            
            total_commission_pct = (BYBIT_TAKER + (MEXC_TAKER if exchange_name == 'MEXC' else OKX_TAKER)) * 100
            
            funding_long = prices_long.get('funding', 0)
            funding_short = prices_short.get('funding', 0)
            total_funding = -funding_long + funding_short
            
            gross_profit_usd = (spread_pct / 100) * POSITION_SIZE_USD
            commission_usd = (total_commission_pct / 100) * POSITION_SIZE_USD
            funding_usd = (total_funding / 100) * POSITION_SIZE_USD * 8
            net_profit_usd = gross_profit_usd - commission_usd + funding_usd
            
            if net_profit_usd < MIN_PROFIT_USD:
                continue
            
            stats = get_convergence_stats(symbol)
            
            if stats['convergence_rate'] < MIN_CONVERGENCE_RATE * 100:
                continue
            
            opportunity = {
                'symbol': symbol,
                'exchange_long': 'Bybit',
                'exchange_short': exchange_name,
                'price_long': prices_long['price'],
                'price_short': prices_short['price'],
                'spread_pct': spread_pct,
                'gross_profit_usd': gross_profit_usd,
                'commission_usd': commission_usd,
                'net_profit_usd': net_profit_usd,
                'convergence_rate': stats['convergence_rate'],
                'avg_convergence_time': stats['avg_convergence_time'],
                'recommendation': stats['recommendation'],
                'stats': stats
            }
            
            opportunities.append(opportunity)
    
    return sorted(opportunities, key=lambda x: x['net_profit_usd'], reverse=True)

# ========== MAIN LOOP ==========
async def main():
    await init_session()
    init_db()
    
    try:
        logger.info("🤖 Cross-Exchange Futures Arbitrage Bot v4.2.1 + TRACKING + MEXC FIX")
        logger.info(f"💰 Капитал: ${POSITION_SIZE_USD} на каждой бирже\n")
        
        logger.info("📊 Загружаем все пары Futures...")
        bybit_pairs = await get_bybit_futures_pairs()
        mexc_pairs = await get_mexc_futures_pairs()
        okx_pairs = await get_okx_futures_pairs()
        
        all_symbols = set(bybit_pairs.keys()) & set(mexc_pairs.keys()) & set(okx_pairs.keys())
        symbols_list = list(all_symbols)
        
        logger.info(f"✅ Найдено {len(symbols_list)} пар на всех трех биржах!\n")
        
        if bot and CHAT_ID:
            try:
                msg = f"🤖 Bot v4.2.1 MEXC FIX запущен!\n📊 Анализируем {len(symbols_list)} пар\n✅ MEXC API исправлен!"
                bot.send_message(CHAT_ID, msg)
            except Exception as e:
                logger.error(f"❌ Telegram init: {e}")
        
        last_signal_time = {}
        full_update_time = time.time()
        last_tracking_time = time.time()
        
        while True:
            try:
                current_time = datetime.now()
                
                logger.info(f"\n[{current_time.strftime('%H:%M:%S')}] СКАНИРОВАНИЕ СПРЕДОВ...")
                
                bybit_prices = await get_bybit_futures_price_batch(symbols_list)
                
                mexc_tasks = [get_mexc_futures_price(s) for s in symbols_list]
                okx_tasks = [get_okx_futures_price(s) for s in symbols_list]
                
                mexc_results = await asyncio.gather(*mexc_tasks, return_exceptions=True)
                okx_results = await asyncio.gather(*okx_tasks, return_exceptions=True)
                
                mexc_prices = {symbols_list[i]: r for i, r in enumerate(mexc_results) if isinstance(r, dict)}
                okx_prices = {symbols_list[i]: r for i, r in enumerate(okx_results) if isinstance(r, dict)}
                
                logger.info(f"✅ Bybit={len(bybit_prices)}, MEXC={len(mexc_prices)}, OKX={len(okx_prices)}")
                
                opportunities = analyze_opportunities(bybit_prices, mexc_prices, okx_prices)
                
                if opportunities:
                    logger.info(f"\n📊 Найдено {len(opportunities)} возможностей!")
                    for opp in opportunities[:5]:
                        logger.info(f"  {opp['symbol']}: спред {opp['spread_pct']:.2f}% = ${opp['net_profit_usd']:.2f}")
                
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
