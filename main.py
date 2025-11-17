# main.py
# Super-Pro TIDE Bot — Coinbase data, 4h/1h/15m, 1 signal per coin/day
# Uses Telegram bot webhooks for Render Free

import os
import time
import json
import threading
from collections import deque
from datetime import datetime, timezone

import requests
from flask import Flask, request
from telegram import Bot, Update

# ---------- CONFIG (set these as Render environment variables) ----------
BOT_TOKEN = os.environ.get("8249361193:AAHiuDvhZpCEdZ3EhLoFAX_liNPz5-zWA5c")
CHAT_ID   = os.environ.get("7520425790")
REPO_NAME = os.environ.get("REPO_NAME", "super-pro-tide-bot")
STATE_FILE = "render_state.json"

# Coinbase endpoints
COINBASE_KLINES = "https://api.exchange.coinbase.com/products/{symbol}/candles"
SYMBOLS = ["BTC-USD","ETH-USD","BNB-USD","SOL-USD","XRP-USD"]
LABELS  = {"BTC-USD":"BTCUSDT","ETH-USD":"ETHUSDT","BNB-USD":"BNBUSDT","SOL-USD":"SOLUSDT","XRP-USD":"XRPUSDT"}

# Timeframes (seconds)
LTF_SEC = 15*60     # 15m
MTF_SEC = 60*60     # 1h
HTF_SEC = 4*60*60   # 4h
GRANULARITIES = {"15m":900,"1h":3600,"4h":14400}

# Strategy constants
EMA_FAST=9; EMA_SLOW=21; RSI_PERIOD=14
PINBAR_RATIO = 3.0
SR_TOP_N = 3
SR_TOUCH_PCT = 0.25
BREAKOUT_PCT = 0.5
RETEST_PCT = 1.0
TP1_PCT = 0.6; TP2_PCT = 1.6; TP3_PCT = 3.5; SL_PCT = 0.7
COOLDOWNS = {"pa": 60*60*6, "break_retest":60*60*6, "pump":60*60}
REQUEST_TIMEOUT = 10

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Set BOT_TOKEN and CHAT_ID environment variables before running.")

# ---------- In-memory stores ----------
htf_candles = {s: deque(maxlen=400) for s in SYMBOLS}
mtf_candles = {s: deque(maxlen=800) for s in SYMBOLS}
ltf_closes  = {s: deque(maxlen=2000) for s in SYMBOLS}
last_signal_time = {s: {} for s in SYMBOLS}
daily_sent = {}
lock = threading.Lock()

bot = Bot(token=BOT_TOKEN)
app = Flask("superpro-tide")

# ---------- Helpers ----------
def save_state():
    try:
        with lock:
            with open(STATE_FILE,"w") as f:
                json.dump({"daily_sent": daily_sent, "last_signal_time": last_signal_time}, f)
    except Exception as e:
        print("save_state error:", e)

def load_state():
    global daily_sent, last_signal_time
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE,"r") as f:
                obj = json.load(f)
                daily_sent = obj.get("daily_sent", {})
                last_signal_time.update(obj.get("last_signal_time", {}))
        except Exception as e:
            print("load_state error:", e)

def coinbase_klines(symbol, granularity, limit=200):
    params = {"granularity": granularity, "limit": limit}
    url = COINBASE_KLINES.format(symbol=symbol)
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            out = []
            for row in reversed(data):
                ts = int(row[0])
                low = float(row[1]); high = float(row[2]); open_ = float(row[3]); close = float(row[4])
                out.append((open_, high, low, close, ts))
            return out
        except Exception as e:
            time.sleep(0.4)
    return None

def compute_ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    ema = sum(values[:period])/period
    for v in values[period:]:
        ema = v*k + ema*(1-k)
    return ema

def compute_RSI(values, period=14):
    if len(values) < period+1: return None
    gains=losses=0.0
    for i in range(-period,0):
        diff = values[i] - values[i-1]
        if diff>0: gains += diff
        else: losses += abs(diff)
    avg_gain = gains/period
    avg_loss = losses/period if losses>0 else 1e-9
    rs = avg_gain/avg_loss
    return 100 - (100/(1+rs))

def top_n_sr(htf_list, n=3):
    highs = sorted({c[1] for c in htf_list}, reverse=True)[:n]
    lows  = sorted({c[2] for c in htf_list})[:n]
    return lows, highs

def is_touch(price, level, pct=SR_TOUCH_PCT):
    return abs(price-level)/level*100 <= pct

def detect_pinbar_from_closes(closes):
    if len(closes) < 3: return False
    a,b,c = closes[-3], closes[-2], closes[-1]
    body = abs(c-b)
    if body == 0: return False
    wick_top = max(b,c) - a
    wick_bot = a - min(b,c)
    return (wick_top > body * PINBAR_RATIO) or (wick_bot > body * PINBAR_RATIO)

def detect_engulfing(closes):
    if len(closes) < 3: return (False, None)
    prev_prev, prev, curr = closes[-3], closes[-2], closes[-1]
    prev_body = abs(prev - prev_prev)
    curr_body = abs(curr - prev)
    if prev_body == 0: return (False,None)
    if curr_body > prev_body:
        return (True, "bull" if curr > prev else "bear")
    return (False,None)

def sl_tp(price, side):
    if side=="BUY":
        tp1 = round(price*(1+TP1_PCT/100),6); tp2 = round(price*(1+TP2_PCT/100),6); tp3 = round(price*(1+TP3_PCT/100),6)
        sl  = round(price*(1-SL_PCT/100),6)
    else:
        tp1 = round(price*(1-TP1_PCT/100),6); tp2 = round(price*(1-TP2_PCT/100),6); tp3 = round(price*(1-TP3_PCT/100),6)
        sl  = round(price*(1+SL_PCT/100),6)
    return tp1,tp2,tp3,sl

def can_send(symbol, key):
    now=time.time()
    last = last_signal_time.get(symbol,{}).get(key,0)
    cooldown = COOLDOWNS.get(key,60)
    if now-last>cooldown:
        last_signal_time.setdefault(symbol,{})[key]=now
        return True
    return False

def send_telegram_message(text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for _ in range(2):
        try:
            r = requests.post(url, json={"chat_id":CHAT_ID,"text":text,"parse_mode":parse_mode}, timeout=8)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False

# ---------- Analysis & signal logic ----------
# (reuse all your functions: breakout_and_retest, analyze_symbol etc.)
# For brevity, you can copy the logic from your old main.py here

# ---------- Flask Webhook ----------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    # Here you can call your analyze_symbol or send status messages
    # Example: send a heartbeat
    send_telegram_message("🤖 Super-Pro TIDE Bot received an update")
    return "OK"

@app.route("/")
def health():
    return f"{REPO_NAME}: OK"

# ---------- Worker to fetch Coinbase data every 5 min ----------
def worker_loop():
    load_state()
    while True:
        for s in SYMBOLS:
            try:
                htf = coinbase_klines(s, GRANULARITIES["4h"], limit=6)
                mtf = coinbase_klines(s, GRANULARITIES["1h"], limit=6)
                ltf = coinbase_klines(s, GRANULARITIES["15m"], limit=6)
                if htf: 
                    for c in htf:
                        if not htf_candles[s] or c[4] > htf_candles[s][-1][4]:
                            htf_candles[s].append(c)
                if mtf: 
                    for c in mtf:
                        if not mtf_candles[s] or c[4] > mtf_candles[s][-1][4]:
                            mtf_candles[s].append(c)
                if ltf: 
                    for c in ltf:
                        if not ltf_closes[s] or c[3] != ltf_closes[s][-1]:
                            ltf_closes[s].append(c[3])
                # call analyze_symbol(s) here
            except Exception as e:
                print("worker error", s, e)
        save_state()
        time.sleep(300)

# Start worker thread
threading.Thread(target=worker_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
