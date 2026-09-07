import os
import sys
import time
import requests

# ── Конфиг из переменных окружения (Railway → Variables) ──────────────
def need(key):
    v = os.environ.get(key)
    if not v:
        print(f"ОШИБКА: не задана переменная {key}", flush=True)
        sys.exit(1)
    return v

TG_TOKEN   = need("BOT_TOKEN")
TG_CHAT    = need("CHAT_ID")
PROXY_URL  = os.environ.get("PROXY_URL", "https://bybit-proxy.aleks000009.workers.dev")
CATEGORY   = os.environ.get("CATEGORY", "spot")               # spot | linear
SPREAD_MIN = float(os.environ.get("SPREAD_THRESHOLD", "0.5")) # % — порог сигнала
TURN_MIN   = float(os.environ.get("MIN_VOL_USD", "2000000"))  # USDT — отсекаем мёртвые пары
POLL_SEC   = int(os.environ.get("SCAN_EVERY", "10"))          # период опроса, сек
TOP_N      = int(os.environ.get("TOP_N", "10"))               # макс. сигналов за один проход
COOLDOWN   = int(os.environ.get("ALERT_COOLDOWN", "300"))     # пауза по одному символу, сек

_last_alert = {}   # symbol -> ts последнего сигнала


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log(f"telegram error: {e}")


def fetch_tickers():
    r = requests.get(f"{PROXY_URL}/tickers", params={"category": CATEGORY}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={data.get('retCode')} {data.get('retMsg')}")
    return data["result"]["list"]


def scan():
    hits = []
    for t in fetch_tickers():
        try:
            bid = float(t.get("bid1Price") or 0)
            ask = float(t.get("ask1Price") or 0)
            turn = float(t.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        if turn < TURN_MIN:
            continue
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid * 100
        if spread >= SPREAD_MIN:
            hits.append((spread, t["symbol"], bid, ask, turn))
    hits.sort(reverse=True)
    return hits


def main():
    log(f"start | {CATEGORY} | порог {SPREAD_MIN}% | оборот>{TURN_MIN:.0f} | опрос {POLL_SEC}с")
    send_telegram(f"🟢 Spread-скринер запущен\nКатегория: {CATEGORY}\nПорог: {SPREAD_MIN}%")
    while True:
        try:
            now = time.time()
            sent = 0
            for spread, sym, bid, ask, turn in scan():
                if sent >= TOP_N:
                    break
                if now - _last_alert.get(sym, 0) < COOLDOWN:
                    continue
                _last_alert[sym] = now
                sent += 1
                send_telegram(
                    f"⚡️ <b>{sym}</b>  спред <b>{spread:.2f}%</b>\n"
                    f"bid {bid:g} / ask {ask:g}\n"
                    f"оборот 24ч: {turn:,.0f} USDT"
                )
                log(f"ALERT {sym} {spread:.2f}%")
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
