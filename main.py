#!/usr/bin/env python3
"""
Cross-Exchange Arbitrage Bot v3.1
Мини-исправления на основе кода-ревью:
✅ MIN_SPREAD_PCT используется правильно
✅ Bybit /tickers без symbol (1 запрос вместо 100!)
✅ Semaphore(3) вместо (5)
✅ Sleep 60 сек вместо 30
✅ MAX_RETRIES = 5 для надежности
"""

import os
import time
import json
import logging
import math
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, List, Dict
import aiohttp
import requests
import telebot

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arb_bot_v3.1.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГ ==========
BYBIT_API = "https://api.bybit.com/v5"
MEXC_API = "https://api.mexc.com/api/v3"
OKX_API = "https://www.okx.com/api/v5"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Параметры торговли
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", "2.0"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "1.0"))
MAX_VOLATILITY_PCT = float(os.environ.get("MAX_VOLATILITY_PCT", "3.0"))
POSITION_SIZE_USD = float(os.environ.get("POSITION_SIZE_USD", "100"))

# Комиссии (TAKER)
BYBIT_TAKER = 0.0015
MEXC_TAKER = 0.0015
OKX_TAKER = 0.0015

# Комиссии вывода
WITHDRAWAL_FEE_USD = {
    'BTC': 5.0,
    'ETH': 2.0,
    'SOL': 0.1,
    'BNB': 0.05,
    'USDT': 1.0,
    'USDC': 1.0,
    'default': 1.5
}

# Время вывода (минуты)
WITHDRAWAL_TIME = {
    'BTC': 40,
    'ETH': 25,
    'SOL': 10,
    'BNB': 5,
    'default': 45
}

# Минимальный объём
MIN_VOLUME_24H = 50000

# RETRY - ПОВЫШЕНО
REQUEST_TIMEOUT = 10
MAX_RETRIES = 5  # было 3, теперь 5 для надежности
RETRY_DELAY_BASE = 0.5

# CYCLE INTERVAL - ПОВЫШЕНО
SCAN_INTERVAL_SEC = 60  # было 30, теперь 60

bot = telebot.TeleBot(BOT_TOKEN, threaded=False) if BOT_TOKEN else None

# Хранилище цен
price_history = defaultdict(list)

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

# ========== SEMAPHORE - СНИЖЕНО ==========
semaphore_bybit = asyncio.Semaphore(3)   # было 5, теперь 3 (безопаснее)
semaphore_mexc = asyncio.Semaphore(3)    # было 5, теперь 3
semaphore_okx = asyncio.Semaphore(3)     # было 5, теперь 3

# ========== API ==========
async def make_request_with_retry(session, url, params=None, exchange=""):
    """Запрос с retry"""
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    wait_time = RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(f"⏱️ {exchange}: rate limit, retry за {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"⚠️ {exchange}: HTTP {resp.status}")
                    return None
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY_BASE * (2 ** attempt)
                logger.warning(f"⏱️ {exchange}: timeout, retry за {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"❌ {exchange}: timeout после {MAX_RETRIES} попыток")
        except aiohttp.ClientError as e:
            logger.error(f"❌ {exchange} ClientError: {type(e).__name__}")
            return None
    
    return None

async def get_bybit_symbols() -> List[str]:
    """Получить все пары Bybit Spot"""
    try:
        async with semaphore_bybit:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{BYBIT_API}/market/instruments-info",
                params={"category": "spot"},
                exchange="Bybit"
            )
            
            if data and data.get("retCode") == 0:
                symbols = []
                for item in data.get("result", {}).get("list", []):
                    symbol = item.get("symbol", "")
                    if "USDT" in symbol:
                        symbols.append(symbol)
                logger.info(f"✅ Bybit: {len(symbols)} символов")
                return symbols
    except Exception as e:
        logger.error(f"❌ Bybit symbols: {e}")
    
    return []

async def get_mexc_symbols() -> List[str]:
    """Получить все пары MEXC Spot"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/exchangeInfo",
                exchange="MEXC"
            )
            
            if data:
                symbols = []
                for item in data.get("symbols", []):
                    symbol = item.get("symbol", "")
                    if symbol.endswith("USDT"):
                        symbols.append(symbol)
                logger.info(f"✅ MEXC: {len(symbols)} символов")
                return symbols
    except Exception as e:
        logger.error(f"❌ MEXC symbols: {e}")
    
    return []

async def get_okx_symbols() -> List[str]:
    """Получить все пары OKX Spot"""
    try:
        async with semaphore_okx:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{OKX_API}/public/instruments",
                params={"instType": "SPOT"},
                exchange="OKX"
            )
            
            if data and data.get("code") == "0":
                symbols = []
                for item in data.get("data", []):
                    inst_id = item.get("instId", "")
                    if "-USDT" in inst_id:
                        standard_symbol = inst_id.replace("-USDT", "USDT")
                        symbols.append(standard_symbol)
                logger.info(f"✅ OKX: {len(symbols)} символов")
                return symbols
    except Exception as e:
        logger.error(f"❌ OKX symbols: {e}")
    
    return []

# ========== ПОЛУЧЕНИЕ ЦЕНЫ - ОПТИМИЗИРОВАНО ==========
async def get_bybit_prices_batch(symbols: List[str]) -> Dict[str, Optional[Dict]]:
    """Получить цены для ВСЕХ символов за 1 запрос (оптимизация!)"""
    result = {}
    try:
        async with semaphore_bybit:
            sess = await init_session()
            # Получаем все пары сразу БЕЗ параметра symbol
            data = await make_request_with_retry(
                sess,
                f"{BYBIT_API}/market/tickers",
                params={"category": "spot"},
                exchange="Bybit"
            )
            
            if data and data.get("retCode") == 0:
                # Создаём map по символам
                price_map = {}
                for item in data.get("result", {}).get("list", []):
                    symbol = item.get("symbol", "")
                    price_map[symbol] = {
                        'price': float(item.get('lastPrice', 0)),
                        'bid': float(item.get('bid1Price', 0)),
                        'ask': float(item.get('ask1Price', 0)),
                        'volume': float(item.get('volume24h', 0))
                    }
                
                # Берём только нужные символы
                for symbol in symbols:
                    result[symbol] = price_map.get(symbol)
                
                logger.debug(f"📊 Bybit: получено {len(result)} цен")
    except Exception as e:
        logger.debug(f"⚠️ Bybit batch: {type(e).__name__}")
    
    return result

async def get_mexc_price(symbol: str) -> Optional[Dict]:
    """Получить цену MEXC Spot"""
    try:
        async with semaphore_mexc:
            sess = await init_session()
            data = await make_request_with_retry(
                sess,
                f"{MEXC_API}/ticker/24hr",
                params={"symbol": symbol},
                exchange="MEXC"
            )
            
            if data and data.get("symbol"):
                return {
                    'price': float(data.get('lastPrice', 0)),
                    'bid': float(data.get('bidPrice', 0)),
                    'ask': float(data.get('askPrice', 0)),
                    'volume': float(data.get('quoteAssetVolume', 0))
                }
    except Exception as e:
        logger.debug(f"⚠️ MEXC {symbol}: {type(e).__name__}")
    
    return None

async def get_okx_price(symbol: str) -> Optional[Dict]:
    """Получить цену OKX Spot"""
    okx_symbol = symbol.replace("USDT", "-USDT")
    
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
                    'bid': float(item.get('bidPx', 0)),
                    'ask': float(item.get('askPx', 0)),
                    'volume': float(item.get('vol24h', 0))
                }
    except Exception as e:
        logger.debug(f"⚠️ OKX {symbol}: {type(e).__name__}")
    
    return None

# ========== АНАЛИЗ ==========
def get_common_symbols(bybit: List[str], mexc: List[str], okx: List[str]) -> List[str]:
    """Найти пересечение"""
    common = set(bybit) & set(mexc) & set(okx)
    result = sorted(list(common))
    logger.info(f"📊 Найдено {len(result)} монет на всех трех!")
    return result

def record_price(symbol: str, price: float):
    """Записать цену"""
    price_history[symbol].append({
        'price': price,
        'time': datetime.now()
    })
    
    cutoff = datetime.now() - timedelta(hours=1.5)
    price_history[symbol] = [p for p in price_history[symbol] if p['time'] > cutoff]

def calculate_volatility(symbol: str) -> Optional[float]:
    """Волатильность за последний час"""
    if symbol not in price_history or len(price_history[symbol]) < 5:
        return None
    
    prices = [p['price'] for p in price_history[symbol]]
    if len(prices) < 2:
        return None
    
    min_price = min(prices)
    max_price = max(prices)
    avg_price = sum(prices) / len(prices)
    
    if avg_price == 0:
        return None
    
    return ((max_price - min_price) / avg_price) * 100

def get_withdrawal_fee_usd(symbol: str) -> float:
    """Комиссия вывода"""
    base_symbol = symbol.replace('USDT', '')
    return WITHDRAWAL_FEE_USD.get(base_symbol, WITHDRAWAL_FEE_USD['default'])

def get_withdrawal_time_min(symbol: str) -> int:
    """Время вывода"""
    base_symbol = symbol.replace('USDT', '')
    return WITHDRAWAL_TIME.get(base_symbol, WITHDRAWAL_TIME['default'])

def analyze_arbitrage(symbol: str, bybit: Optional[Dict], mexc: Optional[Dict], 
                     okx: Optional[Dict]) -> List[Dict]:
    """Анализируем: Bybit дешево → MEXC/OKX дорого"""
    
    if not (bybit and mexc and okx):
        return []
    
    # Фильтруем по объёму
    if bybit.get('volume', 0) < MIN_VOLUME_24H or mexc.get('volume', 0) < MIN_VOLUME_24H or okx.get('volume', 0) < MIN_VOLUME_24H:
        return []
    
    opportunities = []
    
    pairs = [
        ('MEXC', bybit['ask'], mexc['bid'], BYBIT_TAKER, MEXC_TAKER),
        ('OKX', bybit['ask'], okx['bid'], BYBIT_TAKER, OKX_TAKER)
    ]
    
    withdrawal_fee = get_withdrawal_fee_usd(symbol)
    withdrawal_time = get_withdrawal_time_min(symbol)
    volatility = calculate_volatility(f"{symbol}_Bybit")
    
    for sell_exchange, buy_price, sell_price, buy_comm, sell_comm in pairs:
        if buy_price <= 0 or sell_price <= 0:
            continue
        
        # ✅ ИСПРАВЛЕНИЕ: используем MIN_SPREAD_PCT (не 0.5!)
        spread_pct = ((sell_price - buy_price) / buy_price) * 100
        
        if spread_pct < MIN_SPREAD_PCT:  # ПРАВИЛЬНО!
            continue
        
        # Комиссии
        total_commission_pct = (buy_comm + sell_comm) * 100
        withdrawal_fee_pct = (withdrawal_fee / buy_price) * 100
        
        # Волатильность
        expected_loss_pct = 0
        if volatility:
            time_ratio = withdrawal_time / 60
            expected_vol = volatility * math.sqrt(time_ratio)
            expected_loss_pct = expected_vol / 2
        
        # Расчёты
        gross_profit_pct = spread_pct
        total_costs_pct = total_commission_pct + withdrawal_fee_pct
        net_profit_pct = gross_profit_pct - total_costs_pct - expected_loss_pct
        
        # В USD
        gross_profit_usd = (spread_pct / 100) * POSITION_SIZE_USD
        total_costs_usd = (total_costs_pct / 100) * POSITION_SIZE_USD
        expected_loss_usd = (expected_loss_pct / 100) * POSITION_SIZE_USD
        net_profit_usd = gross_profit_usd - total_costs_usd - expected_loss_usd
        
        # Статус
        profit_risk_ratio = net_profit_usd / expected_loss_usd if expected_loss_usd > 0 else 999
        
        if net_profit_usd < MIN_PROFIT_USD:
            status = '🔴 НЕ_ВЫГОДНО'
        elif volatility and volatility > MAX_VOLATILITY_PCT:
            status = '⚫ ВЫСОКИЙ_РИСК'
        elif profit_risk_ratio < 2:
            status = '🟡 РИСК_ЕСТЬ'
        else:
            status = '🟢 ПЕРЕВОДИТЬ'
        
        opportunity = {
            'route': f'Bybit→{sell_exchange}',
            'buy_exchange': 'Bybit',
            'buy_price': buy_price,
            'sell_exchange': sell_exchange,
            'sell_price': sell_price,
            'spread_pct': spread_pct,
            'gross_profit_usd': gross_profit_usd,
            'total_costs_usd': total_costs_usd,
            'expected_loss_usd': expected_loss_usd,
            'net_profit_usd': net_profit_usd,
            'withdrawal_time': withdrawal_time,
            'volatility': volatility,
            'profit_risk_ratio': profit_risk_ratio,
            'status': status
        }
        
        opportunities.append(opportunity)
    
    return sorted(opportunities, key=lambda x: x['net_profit_usd'], reverse=True)

# ========== СИГНАЛЫ ==========
def send_signal(symbol: str, opportunity: Dict):
    """Отправить сигнал"""
    emoji = '🟢' if opportunity['status'] == '🟢 ПЕРЕВОДИТЬ' else '🟡' if 'РИСК' in opportunity['status'] else '🔴'
    
    msg = f"{emoji} АРБИТРАЖ BYBIT → {opportunity['sell_exchange']}\n\n"
    msg += f"💰 {symbol}\n"
    msg += f"{'─' * 40}\n\n"
    msg += f"Bybit (дешево): ${opportunity['buy_price']:.6f}\n"
    msg += f"{opportunity['sell_exchange']} (дорого): ${opportunity['sell_price']:.6f}\n\n"
    msg += f"📊 Спред: {opportunity['spread_pct']:+.2f}%\n"
    msg += f"Валовый: ${opportunity['gross_profit_usd']:.2f}\n"
    msg += f"Комиссии: -${opportunity['total_costs_usd']:.2f}\n"
    
    if opportunity['volatility']:
        msg += f"Волатильность: ±{opportunity['volatility']:.2f}%\n"
        msg += f"Вывод: {opportunity['withdrawal_time']} мин\n"
        msg += f"Риск: -${opportunity['expected_loss_usd']:.2f}\n"
    
    msg += f"\n💡 ИТОГО: ${opportunity['net_profit_usd']:.2f} ✅\n"
    msg += f"Статус: {opportunity['status']}\n"
    
    if bot and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, msg)
        except Exception as e:
            logger.error(f"❌ Telegram: {e}")
    
    logger.info(msg)

# ========== MAIN ==========
async def main():
    await init_session()
    
    try:
        logger.info("🤖 Cross-Exchange Arbitrage Bot v3.1")
        logger.info(f"💰 Капитал: ${POSITION_SIZE_USD} на Bybit")
        logger.info(f"📊 Минимальный спред: {MIN_SPREAD_PCT}%")
        logger.info(f"⏰ Интервал сканирования: {SCAN_INTERVAL_SEC} сек")
        logger.info(f"🔧 Semaphore: 3/3/3, MAX_RETRIES: {MAX_RETRIES}\n")
        
        # Загружаем символы
        logger.info("📊 Загружаем символы...")
        bybit_syms = await get_bybit_symbols()
        mexc_syms = await get_mexc_symbols()
        okx_syms = await get_okx_symbols()
        
        SYMBOLS = get_common_symbols(bybit_syms, mexc_syms, okx_syms)
        
        logger.info(f"✅ Мониторим {len(SYMBOLS)} монет!")
        logger.info(f"Примеры: {', '.join(SYMBOLS[:15])}\n")
        
        if bot and CHAT_ID:
            try:
                msg = f"🤖 Bot v3.1 запущен!\n💰 $100 на Bybit\n📊 Мониторим {len(SYMBOLS)} монет\n✅ Стратегия: Bybit→MEXC/OKX"
                bot.send_message(CHAT_ID, msg)
            except Exception as e:
                logger.error(f"❌ Telegram init: {e}")
        
        last_signal_time = {}
        
        while True:
            try:
                cycle_start = time.time()
                logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] Сканируем {len(SYMBOLS)} монет...")
                
                # Параллельные запросы - ОПТИМИЗИРОВАНО
                # Bybit - батч запрос (все сразу!)
                bybit_prices = await get_bybit_prices_batch(SYMBOLS)
                
                # MEXC и OKX - отдельно по монетам
                tasks = []
                for symbol in SYMBOLS:
                    tasks.append(get_mexc_price(symbol))
                    tasks.append(get_okx_price(symbol))
                
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Обработка результатов
                for i, symbol in enumerate(SYMBOLS):
                    bybit = bybit_prices.get(symbol)
                    
                    mexc_idx = i * 2
                    okx_idx = i * 2 + 1
                    
                    mexc = results[mexc_idx] if isinstance(results[mexc_idx], dict) else None
                    okx = results[okx_idx] if isinstance(results[okx_idx], dict) else None
                    
                    if bybit and mexc and okx:
                        # Записываем цены
                        record_price(f"{symbol}_Bybit", bybit['price'])
                        record_price(f"{symbol}_MEXC", mexc['price'])
                        record_price(f"{symbol}_OKX", okx['price'])
                        
                        # Анализируем
                        opportunities = analyze_arbitrage(symbol, bybit, mexc, okx)
                        
                        if opportunities and opportunities[0]['net_profit_usd'] > MIN_PROFIT_USD:
                            best = opportunities[0]
                            signal_key = f"{symbol}_{best['route']}"
                            
                            # Не спамим
                            if signal_key not in last_signal_time or (time.time() - last_signal_time[signal_key]) > 300:
                                send_signal(symbol, best)
                                last_signal_time[signal_key] = time.time()
                
                cycle_time = time.time() - cycle_start
                logger.info(f"✅ Цикл завершен за {cycle_time:.1f}s")
                
                # ИСПРАВЛЕНИЕ: используем SCAN_INTERVAL_SEC
                await asyncio.sleep(SCAN_INTERVAL_SEC)
            
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
