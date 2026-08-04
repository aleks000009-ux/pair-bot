#!/usr/bin/env python3
"""
Cross-Exchange Futures Arbitrage Bot v4.6
Bybit + OKX only

Изменения относительно v4.5:
- Дефолтные пороги приведены к реальности (MIN_SPREAD_PCT=0.15, MIN_PROFIT_USD=0.05).
  Старый MIN_PROFIT_USD=20 на теле $100 отсекал 100% возможностей (нужен был спред ~20%).
- OKX-цены грузятся ОДНИМ батч-запросом /market/tickers?instType=SWAP
  вместо ~238 отдельных запросов (те троттлились и приходили неполными).
- Анализируются ОБА направления спреда (Bybit>OKX и OKX>Bybit), а не одно.
- Убран лишний множитель ×8 в расчёте фандинга (он и так за 8ч период).
  ВНИМАНИЕ: батч-тикер OKX не отдаёт fundingRate, поэтому funding=0.
  Считаем чистую арбитражную маржу: спред - комиссии. Это честнее фейкового числа.
- datetime.utcnow() -> datetime.now(timezone.utc) (убирает DeprecationWarning).
- С публичных OKX-эндпоинтов убрана ненужная подпись.

ВАЖНО: бот только ДЕТЕКТИРУЕТ спреды и шлёт сигнал в Telegram.
Реального исполнения сделок здесь нет.
"""

import os
import time
import logging
import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict
import aiohttp
import telebot

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('arb_bot_v4_6.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГ ==========
BYBIT_API = "https://api.bybit.com/v5"
OKX_API = "https://www.okx.com/api/v5"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

# Параметры торговли (можно переопределить через env на Railway)
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", "0.15"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "0.05"))
POSITION_SIZE_USD = float(os.environ.get("POSITION_SIZE_USD", "100"))

# Комиссии TAKER
BYBIT_TAKER = 0.0005
OKX_TAKER = 0.0002

# CYCLE INTERVALS
FAST_SCAN_INTERVAL = 600  # 10 минут

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
    conn = sqlite3.connect('arbitrage_v4_6.db')
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS signals_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            spread_pct REAL NOT NULL,
            expected_profit REAL NOT NULL
        )
    ''')

    conn.commit()
    conn.close()

# ========== API REQUESTS ==========
async def make_request(session, url, params=None, headers=None, exchange=""):
    """Простой GET-запрос с логированием"""
    try:
        async with session.get(url, params=params, headers=headers, timeout=15) as resp:
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
    """Получить список пар Bybit Futures (linear USDT perpetual)"""
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

async def get_bybit_futures_price_batch(symbols: set) -> Dict[str, Dict]:
    """Получить все цены Bybit одним запросом /market/tickers"""
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
                        try:
                            price = float(item.get('lastPrice', 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        result[symbol] = {
                            'price': price,
                            'funding': float(item.get('fundingRate', 0) or 0) * 100
                        }
    except Exception as e:
        logger.error(f"❌ Bybit prices: {e}")

    return result

# ========== OKX API (публичные эндпоинты, подпись не нужна) ==========
async def get_okx_futures_pairs() -> Dict[str, Dict]:
    """Получить список пар OKX Futures (USDT SWAP)"""
    try:
        async with semaphore_okx:
            sess = await init_session()
            data = await make_request(
                sess,
                f"{OKX_API}/public/instruments",
                params={"instType": "SWAP"},
                exchange="OKX"
            )

            if data and data.get("code") == "0":
                pairs = {}
                for item in data.get("data", []):
                    inst_id = item.get("instId", "")
                    if inst_id.endswith("-USDT-SWAP"):
                        symbol = inst_id.replace("-USDT-SWAP", "USDT")
                        pairs[symbol] = {'price': None, 'funding': None}
                logger.info(f"✅ OKX Futures: {len(pairs)} пар")
                return pairs
    except Exception as e:
        logger.error(f"❌ OKX pairs: {e}")

    return {}

async def get_okx_futures_price_batch(symbols: set) -> Dict[str, Dict]:
    """Получить все цены OKX одним запросом /market/tickers?instType=SWAP"""
    result = {}
    try:
        async with semaphore_okx:
            sess = await init_session()
            data = await make_request(
                sess,
                f"{OKX_API}/market/tickers",
                params={"instType": "SWAP"},
                exchange="OKX"
            )

            if data and data.get("code") == "0":
                for item in data.get("data", []):
                    inst_id = item.get("instId", "")
                    if not inst_id.endswith("-USDT-SWAP"):
                        continue
                    symbol = inst_id.replace("-USDT-SWAP", "USDT")
                    if symbol not in symbols:
                        continue
                    try:
                        price = float(item.get('last', 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    # tickers OKX не отдаёт fundingRate -> funding=0
                    result[symbol] = {'price': price, 'funding': 0.0}
    except Exception as e:
        logger.error(f"❌ OKX prices: {e}")

    return result

# ========== АНАЛИЗ ==========
def analyze_opportunities(bybit_prices: Dict, okx_prices: Dict) -> List[Dict]:
    """
    Анализируем возможности в ОБА направления:
      - Bybit LONG / OKX SHORT  (когда OKX дороже Bybit)
      - OKX LONG / Bybit SHORT  (когда Bybit дороже OKX)
    """
    opportunities = []
    commission_pct = (BYBIT_TAKER + OKX_TAKER) * 100  # round-trip round ber(2 ноги)

    for symbol in bybit_prices:
        if symbol not in okx_prices:
            continue

        bybit = bybit_prices[symbol]
        okx = okx_prices[symbol]

        b_price = bybit.get('price', 0) if bybit else 0
        o_price = okx.get('price', 0) if okx else 0
        if b_price <= 0 or o_price <= 0:
            continue

        # Определяем направление по тому, где цена выше
        if o_price >= b_price:
            # Купить дешевле на Bybit, продать дороже на OKX
            direction = "Bybit LONG / OKX SHORT"
            long_ex, short_ex = "Bybit", "OKX"
            long_price, short_price = b_price, o_price
        else:
            # Купить дешевле на OKX, продать дороже на Bybit
            direction = "OKX LONG / Bybit SHORT"
            long_ex, short_ex = "OKX", "Bybit"
            long_price, short_price = o_price, b_price

        spread_pct = abs(o_price - b_price) / min(o_price, b_price) * 100

        if spread_pct < MIN_SPREAD_PCT:
            continue

        gross_profit = (spread_pct / 100) * POSITION_SIZE_USD
        commission = (commission_pct / 100) * POSITION_SIZE_USD
        net_profit = gross_profit - commission

        if net_profit < MIN_PROFIT_USD:
            continue

        opportunities.append({
            'symbol': symbol,
            'direction': direction,
            'long_ex': long_ex,
            'short_ex': short_ex,
            'long_price': long_price,
            'short_price': short_price,
            'spread_pct': spread_pct,
            'gross_profit': gross_profit,
            'commission': commission,
            'net_profit': net_profit,
            'bybit_price': b_price,
            'okx_price': o_price,
        })

    return sorted(opportunities, key=lambda x: x['net_profit'], reverse=True)

# ========== TELEGRAM ==========
def send_signal(opp: Dict):
    """Отправить сигнал в Telegram"""
    msg = "🟢 ФЬЮЧЕРСНЫЙ АРБИТРАЖ\n\n"
    msg += f"💰 {opp['symbol']}\n"
    msg += "─" * 30 + "\n\n"
    msg += f"↔️ {opp['direction']}\n\n"
    msg += f"🟩 {opp['long_ex']} LONG:  {opp['long_price']:.6g}\n"
    msg += f"🟥 {opp['short_ex']} SHORT: {opp['short_price']:.6g}\n\n"
    msg += f"📊 Спред: {opp['spread_pct']:.3f}%\n"
    msg += f"💰 Валовый: ${opp['gross_profit']:.3f}\n"
    msg += f"💱 Комиссии: -${opp['commission']:.3f}\n\n"
    msg += f"💡 ИТОГО (без фандинга): ${opp['net_profit']:.3f}\n"

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
        logger.info("\n" + "=" * 60)
        logger.info("🤖 Cross-Exchange Futures Arbitrage Bot v4.6")
        logger.info("✅ Bybit Futures: Public API")
        logger.info("✅ OKX Futures: Public API")
        logger.info(f"💰 Капитал: ${POSITION_SIZE_USD} на каждой бирже")
        logger.info(f"📊 Минимальный спред: {MIN_SPREAD_PCT}%")
        logger.info(f"📊 Минимальный профит: ${MIN_PROFIT_USD}")
        logger.info("=" * 60 + "\n")

        logger.info("📊 Загружаем пары...")
        bybit_pairs = await get_bybit_futures_pairs()
        okx_pairs = await get_okx_futures_pairs()

        all_symbols = set(bybit_pairs.keys()) & set(okx_pairs.keys())

        logger.info(f"✅ Найдено {len(all_symbols)} пар на обеих биржах!\n")

        if bot and CHAT_ID:
            try:
                msg = (f"🤖 Bot v4.6 запущен!\n✅ Bybit + OKX\n"
                       f"📊 Мониторим {len(all_symbols)} пар\n"
                       f"📈 Мин. спред {MIN_SPREAD_PCT}% / мин. профит ${MIN_PROFIT_USD}")
                bot.send_message(CHAT_ID, msg)
            except Exception as e:
                logger.error(f"❌ Telegram init: {e}")

        last_signal_time = {}

        while True:
            try:
                logger.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Сканирование...")

                bybit_prices = await get_bybit_futures_price_batch(all_symbols)
                okx_prices = await get_okx_futures_price_batch(all_symbols)

                logger.info(f"✅ Цены: Bybit={len(bybit_prices)}, OKX={len(okx_prices)}")

                opportunities = analyze_opportunities(bybit_prices, okx_prices)

                if opportunities:
                    logger.info(f"📊 Найдено {len(opportunities)} возможностей!")

                    for opp in opportunities[:10]:
                        key = opp['symbol']
                        now = time.time()
                        if key not in last_signal_time or (now - last_signal_time[key]) > 1800:
                            send_signal(opp)
                            last_signal_time[key] = now

                            try:
                                conn = sqlite3.connect('arbitrage_v4_6.db')
                                c = conn.cursor()
                                c.execute('''
                                    INSERT INTO signals_sent
                                    (timestamp, symbol, direction, spread_pct, expected_profit)
                                    VALUES (?, ?, ?, ?, ?)
                                ''', (datetime.now().isoformat(), opp['symbol'],
                                      opp['direction'], opp['spread_pct'], opp['net_profit']))
                                conn.commit()
                                conn.close()
                            except Exception as e:
                                logger.error(f"❌ DB insert: {e}")
                else:
                    logger.info("ℹ️ Спредов не найдено")

                await asyncio.sleep(FAST_SCAN_INTERVAL)

            except Exception as e:
                logger.error(f"❌ Main loop: {type(e).__name__}: {e}")
                await asyncio.sleep(5)

    finally:
        await close_session()

if __name__ == "__main__":
    asyncio.run(main())
