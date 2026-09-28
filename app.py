import os
import secrets
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(24).replace("=", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
SCAN_LOCK = threading.Lock()

BASE = "https://fapi.binance.com"

WINDOW_HOURS = 4
VOL_RATIO_MIN = 2.0
MAX_PRICE_MOVE = 3.0
MIN_24H_VOLUME = 1_000_000
THREADS = 10
MAX_MCAP = 100_000_000
CG_PAGES = 8

SQUEEZE_ONLY = True
FUNDING_MAX = -0.005
OI_MIN_CHANGE = 0.0


def telegram_request(method, data=None, timeout=30):
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    r = requests.post(
        f"{TELEGRAM_API}/{method}",
        data=data or {},
        timeout=timeout,
    )
    r.raise_for_status()

    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)

    return payload


def send_telegram(text):
    if not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID")

    for i in range(0, len(text), 3900):
        telegram_request(
            "sendMessage",
            {"chat_id": CHAT_ID, "text": text[i:i + 3900]},
        )


def register_webhook():
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        print("Webhook registration skipped: token or RENDER_EXTERNAL_URL is missing.")
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram/webhook"

    try:
        result = telegram_request(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": "true",
            },
        )
        print(f"Telegram webhook registered: {webhook_url}")
        print(f"Telegram response: {result.get('description', 'ok')}")
    except Exception as e:
        print(f"Webhook registration failed: {e}")


def get(path, params=None):
    r = requests.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def perpetual_symbols():
    info = get("/fapi/v1/exchangeInfo")
    return [
        s["symbol"]
        for s in info["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
    ]


def analyze(symbol):
    try:
        k = get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1h", "limit": 169},
        )

        if len(k) < 169:
            return None

        qvol = [float(c[7]) for c in k]
        recent = qvol[-WINDOW_HOURS:]
        base = qvol[:-WINDOW_HOURS]
        avg_block = sum(base) / (len(base) / WINDOW_HOURS)

        if avg_block == 0:
            return None

        ratio = sum(recent) / avg_block
        open_px = float(k[-WINDOW_HOURS][1])
        last_px = float(k[-1][4])
        move = (last_px - open_px) / open_px * 100
        vol24 = sum(qvol[-24:])

        return {
            "symbol": symbol,
            "ratio": ratio,
            "move": move,
            "price": last_px,
            "vol24": vol24,
        }

    except Exception:
        return None


def market_caps():
    caps = {}

    for page in range(1, CG_PAGES + 1):
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                },
                timeout=20,
            )

            if r.status_code == 429:
                print("CoinGecko rate limit: waiting 30 seconds...")
                time.sleep(30)
                continue

            r.raise_for_status()

            for c in r.json():
                sym = (c.get("symbol") or "").upper()
                mc = c.get("market_cap") or 0

                if sym and mc > caps.get(sym, 0):
                    caps[sym] = mc

            time.sleep(2.5)

        except Exception as e:
            print(f"CoinGecko page {page} error: {e}")

    return caps


def base_asset(symbol):
    s = symbol[:-4] if symbol.endswith("USDT") else symbol

    for prefix in ("1000000", "10000", "1000"):
        if s.startswith(prefix):
            return s[len(prefix):]

    return s


def funding_avg(symbol, periods=3):
    try:
        d = get(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": periods},
        )

        rates = [float(x["fundingRate"]) * 100 for x in d]
        return sum(rates) / len(rates) if rates else None

    except Exception:
        return None


def oi_change(symbol):
    try:
        d = get(
            "/futures/data/openInterestHist",
            {
                "symbol": symbol,
                "period": "1h",
                "limit": WINDOW_HOURS + 1,
            },
        )

        if len(d) < 2:
            return None

        first = float(d[0]["sumOpenInterestValue"])
        last = float(d[-1]["sumOpenInterestValue"])

        if first == 0:
            return None

        return (last - first) / first * 100

    except Exception:
        return None


def run_scan():
    print("Loading Binance symbols...")
    symbols = perpetual_symbols()
    print(f"Scanning {len(symbols)} futures pairs...")

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        results = [
            r
            for r in ex.map(analyze, symbols)
            if r is not None
        ]

    funding_data = get("/fapi/v1/premiumIndex")
    funding = {
        f["symbol"]: float(f["lastFundingRate"]) * 100
        for f in funding_data
    }

    caps = {}

    if MAX_MCAP:
        print("Loading CoinGecko market caps...")
        caps = market_caps()

    for r in results:
        r["mcap"] = caps.get(base_asset(r["symbol"]))

    hits = [
        r
        for r in results
        if r["ratio"] >= VOL_RATIO_MIN
        and abs(r["move"]) <= MAX_PRICE_MOVE
        and r["vol24"] >= MIN_24H_VOLUME
        and (
            not MAX_MCAP
            or (
                r["mcap"] is not None
                and r["mcap"] <= MAX_MCAP
            )
        )
    ]

    hits.sort(key=lambda r: r["ratio"], reverse=True)
    symbols_hits = [h["symbol"] for h in hits]

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        ois = list(ex.map(oi_change, symbols_hits))

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        favgs = list(ex.map(funding_avg, symbols_hits))

    for h, oi, fa in zip(hits, ois, favgs):
        h["oi"] = oi
        h["funding"] = funding.get(h["symbol"])
        h["fund_avg"] = fa
        h["squeeze"] = (
            fa is not None
            and fa <= FUNDING_MAX
            and oi is not None
            and oi >= OI_MIN_CHANGE
        )

    if SQUEEZE_ONLY:
        hits = [h for h in hits if h["squeeze"]]

    lines = [
        "🔎 Binance Squeeze Scan",
        f"🕒 {datetime.now():%d/%m/%Y %H:%M}",
        f"נמצאו {len(hits)} מועמדים",
        "",
    ]

    if not hits:
        lines.append("לא נמצא כרגע SQUEEZE שעובר את כל התנאים.")

    for h in hits:
        oi = f"{h['oi']:+.1f}%" if h["oi"] is not None else "n/a"
        fd = f"{h['funding']:+.4f}%" if h["funding"] is not None else "n/a"
        fa = f"{h['fund_avg']:+.4f}%" if h["fund_avg"] is not None else "n/a"
        mc = f"${h['mcap'] / 1e6:.0f}M" if h.get("mcap") else "n/a"

        lines.extend([
            f"🚨 {h['symbol']}",
            f"Volume: x{h['ratio']:.1f}",
            f"Price: {h['move']:+.2f}%",
            f"OI: {oi}",
            f"Funding: {fd}",
            f"Funding 24h avg: {fa}",
            f"Vol24h: ${h['vol24'] / 1e6:.1f}M",
            f"MCap: {mc}",
            "",
        ])

    lines.append(
        "SQUEEZE = נפח קפץ + מחיר יציב + Funding שלילי + OI עולה"
    )

    return "\n".join(lines)


def handle_scan():
    if not SCAN_LOCK.acquire(blocking=False):
        send_telegram("⏳ כבר מתבצעת סריקה. נסי שוב כשהיא תסתיים.")
        return

    try:
        send_telegram("🔎 מתחיל סריקה עכשיו. זה יכול לקחת בערך דקה...")
        result = run_scan()
        send_telegram(result)

    except Exception as e:
        print(f"Scan failed: {e}")
        try:
            send_telegram(f"❌ שגיאה בסריקה:\n{e}")
        except Exception:
            pass

    finally:
        SCAN_LOCK.release()


@app.get("/")
def home():
    return jsonify(
        {
            "ok": True,
            "service": "Binance Telegram Scanner",
            "telegram_configured": bool(BOT_TOKEN and CHAT_ID),
        }
    )


@app.get("/health")
def health():
    return "ok", 200


@app.post("/telegram/webhook")
def telegram_webhook():
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    if received_secret != WEBHOOK_SECRET:
        return "forbidden", 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}

    incoming_chat_id = str(
        (message.get("chat") or {}).get("id", "")
    )

    if incoming_chat_id != CHAT_ID:
        return "ok", 200

    text = (message.get("text") or "").strip()
    command = text.split()[0] if text else ""
    command = command.split("@")[0]

    if command == "/scan":
        threading.Thread(
            target=handle_scan,
            daemon=True,
        ).start()

    elif command == "/start":
        send_telegram(
            "🤖 Binance Squeeze Scanner מחובר לשרת.\n\n"
            "שלחי /scan כדי להריץ סריקה."
        )

    elif command == "/help":
        send_telegram(
            "פקודות זמינות:\n\n"
            "/scan - הרצת סריקה חדשה\n"
            "/status - בדיקת מצב\n"
            "/start - התחלה\n"
            "/help - עזרה"
        )

    elif command == "/status":
        status = "מתבצעת סריקה" if SCAN_LOCK.locked() else "מוכן לסריקה"
        send_telegram(f"✅ השרת פעיל\nמצב: {status}")

    return "ok", 200


register_webhook()
