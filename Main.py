‎# main.py
‎# Super-Pro TIDE Bot — Coinbase data, 4h/1h/15m, 1 signal per coin/day
‎# Uses python-telegram-bot for handlers; background worker uses HTTP Telegram sendMessage for sync safety.
‎
‎import os
‎import time
‎import json
‎import math
‎import requests
‎import threading
‎import statistics
‎from collections import deque
‎from datetime import datetime, timezone
‎from flask import Flask
‎from telegram import Update
‎from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
‎
‎# ---------- CONFIG (set these as Render environment variables) ----------
‎BOT_TOKEN = os.environ.get("8249361193:AAHiuDvhZpCEdZ3EhLoFAX_liNPz5-zWA5c")    # Telegram bot token
‎CHAT_ID   = os.environ.get("7520425790")      
‎REPO_NAME = os.environ.get("Crypto_Signal_bot", "super-pro-tide-bot")
‎STATE_FILE = "render_state.json"
‎# Coinbase endpoints
‎COINBASE_KLINES = "https://api.exchange.coinbase.com/products/{symbol}/candles"
‎# Symbols (Coinbase format)
‎SYMBOLS = ["BTC-USD","ETH-USD","BNB-USD","SOL-USD","XRP-USD"]
‎LABELS  = {"BTC-USD":"BTCUSDT","ETH-USD":"ETHUSDT","BNB-USD":"BNBUSDT","SOL-USD":"SOLUSDT","XRP-USD":"XRPUSDT"}
‎
‎# Timeframes (seconds)
‎LTF_SEC = 15*60     # 15m
‎MTF_SEC = 60*60     # 1h
‎HTF_SEC = 4*60*60   # 4h
‎
‎# Kline granularities (Coinbase uses seconds)
‎GRANULARITIES = {"15m":900,"1h":3600,"4h":14400}
‎
‎# Strategy constants (TIDE)
‎EMA_FAST=9; EMA_SLOW=21; RSI_PERIOD=14
‎PINBAR_RATIO = 3.0           # very strict pinbar
‎SR_TOP_N = 3
‎SR_TOUCH_PCT = 0.25          # strict S/R touch tolerance (%)
‎BREAKOUT_PCT = 0.5           # % breakout threshold
‎RETEST_PCT = 1.0             # % retest tolerance
‎
‎TP1_PCT = 0.6; TP2_PCT = 1.6; TP3_PCT = 3.5; SL_PCT = 0.7
‎
‎# Cooldowns to avoid spamming (still one-signal per day enforces main limit)
‎COOLDOWNS = {"pa": 60*60*6, "break_retest":60*60*6, "pump":60*60}
‎
‎REQUEST_TIMEOUT = 10
‎
‎# ---------- Basic checks ----------
‎if not BOT_TOKEN or not CHAT_ID:
‎    raise SystemExit("Set BOT_TOKEN and CHAT_ID environment variables before running.")
‎
‎# ---------- In-memory stores ----------
‎htf_candles = {s: deque(maxlen=400) for s in SYMBOLS}   # 4h
‎mtf_candles = {s: deque(maxlen=800) for s in SYMBOLS}   # 1h
‎ltf_closes  = {s: deque(maxlen=2000) for s in SYMBOLS}  # 15m closes
‎last_signal_time = {s: {} for s in SYMBOLS}
‎daily_sent = {}   # persisted: { "BTC-USD": "2025-11-09" }
‎
‎lock = threading.Lock()
‎
‎# ---------- Helpers ----------
‎def save_state():
‎    try:
‎        with lock:
‎            with open(STATE_FILE,"w") as f:
‎                json.dump({"daily_sent": daily_sent, "last_signal_time": last_signal_time}, f)
‎    except Exception as e:
‎        print("save_state error:", e)
‎
‎def load_state():
‎    global daily_sent, last_signal_time
‎    if os.path.exists(STATE_FILE):
‎        try:
‎            with open(STATE_FILE,"r") as f:
‎                obj = json.load(f)
‎                daily_sent = obj.get("daily_sent", {})
‎                last_signal_time.update(obj.get("last_signal_time", {}))
‎        except Exception as e:
‎            print("load_state error:", e)
‎
‎def coinbase_klines(symbol, granularity, limit=200):
‎    params = {"granularity": granularity, "limit": limit}
‎    url = COINBASE_KLINES.format(symbol=symbol)
‎    for _ in range(3):
‎        try:
‎            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
‎            r.raise_for_status()
‎            data = r.json()
‎            # Coinbase returns [time, low, high, open, close, volume] — we map to (o,h,l,c,ts)
‎            out = []
‎            for row in reversed(data):  # reverse so newest last
‎                ts = int(row[0])
‎                low = float(row[1]); high = float(row[2]); open_ = float(row[3]); close = float(row[4])
‎                out.append((open_, high, low, close, ts))
‎            return out
‎        except Exception as e:
‎            time.sleep(0.4)
‎    return None
‎
‎def compute_ema(values, period):
‎    if len(values) < period: return None
‎    k = 2/(period+1)
‎    ema = sum(values[:period])/period
‎    for v in values[period:]:
‎        ema = v*k + ema*(1-k)
‎    return ema
‎
‎def compute_RSI(values, period=14):
‎    if len(values) < period+1: return None
‎    gains=losses=0.0
‎    for i in range(-period,0):
‎        diff = values[i] - values[i-1]
‎        if diff>0: gains += diff
‎        else: losses += abs(diff)
‎    avg_gain = gains/period
‎    avg_loss = losses/period if losses>0 else 1e-9
‎    rs = avg_gain/avg_loss
‎    return 100 - (100/(1+rs))
‎
‎def top_n_sr(htf_list, n=3):
‎    highs = sorted({c[1] for c in htf_list}, reverse=True)[:n]
‎    lows  = sorted({c[2] for c in htf_list})[:n]
‎    return lows, highs
‎
‎def is_touch(price, level, pct=SR_TOUCH_PCT):
‎    return abs(price-level)/level*100 <= pct
‎
‎def detect_pinbar_from_closes(closes):
‎    if len(closes) < 3: return False
‎    a,b,c = closes[-3], closes[-2], closes[-1]
‎    body = abs(c-b)
‎    if body == 0: return False
‎    wick_top = max(b,c) - a
‎    wick_bot = a - min(b,c)
‎    return (wick_top > body * PINBAR_RATIO) or (wick_bot > body * PINBAR_RATIO)
‎
‎def detect_engulfing(closes):
‎    if len(closes) < 3: return (False, None)
‎    prev_prev, prev, curr = closes[-3], closes[-2], closes[-1]
‎    prev_body = abs(prev - prev_prev)
‎    curr_body = abs(curr - prev)
‎    if prev_body == 0: return (False,None)
‎    if curr_body > prev_body:
‎        return (True, "bull" if curr > prev else "bear")
‎    return (False,None)
‎
‎def sl_tp(price, side):
‎    if side=="BUY":
‎        tp1 = round(price*(1+TP1_PCT/100),6); tp2 = round(price*(1+TP2_PCT/100),6); tp3 = round(price*(1+TP3_PCT/100),6)
‎        sl  = round(price*(1-SL_PCT/100),6)
‎    else:
‎        tp1 = round(price*(1-TP1_PCT/100),6); tp2 = round(price*(1-TP2_PCT/100),6); tp3 = round(price*(1-TP3_PCT/100),6)
‎        sl  = round(price*(1+SL_PCT/100),6)
‎    return tp1,tp2,tp3,sl
‎
‎def can_send(symbol, key):
‎    now=time.time()
‎    last = last_signal_time.get(symbol,{}).get(key,0)
‎    cooldown = COOLDOWNS.get(key,60)
‎    if now-last>cooldown:
‎        last_signal_time.setdefault(symbol,{})[key]=now
‎        return True
‎    return False
‎
‎def send_telegram_message(text, parse_mode="Markdown"):
‎    # use Telegram HTTP API synchronously so background worker can call easily
‎    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
‎    for _ in range(2):
‎        try:
‎            r = requests.post(url, json={"chat_id":CHAT_ID,"text":text,"parse_mode":parse_mode}, timeout=8)
‎            if r.status_code == 200:
‎                return True
‎        except Exception:
‎            time.sleep(0.3)
‎    return False
‎
‎# ---------- Analysis & signal logic ----------
‎def breakout_and_retest(symbol):
‎    htf = list(htf_candles[symbol])
‎    if len(htf) < 6: return (None,None)
‎    supports, resistances = top_n_sr(htf, SR_TOP_N)
‎    price = htf[-1][3]
‎    # last few LTF closes
‎    ltf = list(ltf_closes[symbol])[-12:]
‎    if not ltf: return (None,None)
‎    for r in resistances:
‎        if price > r*(1+BREAKOUT_PCT/100):
‎            retested = any(p <= r*(1+RETEST_PCT/100) and p >= r*(1-RETEST_PCT/100) for p in ltf)
‎            bounced = ltf[-1] > r
‎            if retested and bounced and can_send(symbol,"break_retest"):
‎                return ("BUY", f"breakout_retest_res:{r}")
‎    for s in supports:
‎        if price < s*(1-BREAKOUT_PCT/100):
‎            retested = any(p >= s*(1-RETEST_PCT/100) and p <= s*(1+RETEST_PCT/100) for p in ltf)
‎            bounced = ltf[-1] < s
‎            if retested and bounced and can_send(symbol,"break_retest"):
‎                return ("SELL", f"breakdown_retest_sup:{s}")
‎    return (None,None)
‎
‎def analyze_symbol(symbol):
‎    htf = list(htf_candles[symbol])
‎    mtf = list(mtf_candles[symbol])
‎    ltf = list(ltf_closes[symbol])
‎    if len(htf) < 8 or len(mtf) < 8 or len(ltf) < 4: return
‎
‎    # HTF indicators
‎    htf_closes = [c[3] for c in htf]
‎    price_htf = htf_closes[-1]
‎    ema_htf_fast = compute_ema(htf_closes, EMA_FAST)
‎    ema_htf_slow = compute_ema(htf_closes, EMA_SLOW)
‎    rsi_htf = compute_RSI(htf_closes, RSI_PERIOD)
‎    trend_up = ema_htf_fast and ema_htf_slow and ema_htf_fast > ema_htf_slow
‎    trend_down = ema_htf_fast and ema_htf_slow and ema_htf_fast < ema_htf_slow
‎
‎    # 1h (mtf) indicators
‎    mtf_closes = [c[3] for c in mtf] if mtf else []
‎    ema_mtf_fast = compute_ema(mtf_closes, EMA_FAST) if mtf_closes else None
‎    ema_mtf_slow = compute_ema(mtf_closes, EMA_SLOW) if mtf_closes else None
‎
‎    # LTF closes for PA detection
‎    ltf_vals = list(ltf)[-10:]
‎
‎    # S/R from HTF
‎    supports, resistances = top_n_sr(htf[-SR_TOP_N:])
‎    touched_support = any(is_touch(price_htf, s) for s in supports) if supports else False
‎    touched_res    = any(is_touch(price_htf, r) for r in resistances) if resistances else False
‎
‎    is_pin = detect_pinbar_from_closes(ltf_vals)
‎    engulfed, e_side = detect_engulfing(ltf_vals)
‎
‎    # 1) Break & retest (highest quality)
‎    br_side, br_reason = breakout_and_retest(symbol)
‎    if br_side=="BUY" and (trend_up or (ema_mtf_fast and ema_mtf_fast>ema_mtf_slow)) and (rsi_htf is None or rsi_htf<=48):
‎        if daily_sent.get(symbol) == datetime.now(timezone.utc).strftime("%Y-%m-%d"): return
‎        tp1,tp2,tp3,sl = sl_tp(price_htf,"BUY")
‎        txt = f\"\"\"🟢 BUY (Break&Retest) — {LABELS[symbol]}
‎Price: {price_htf:.6f}
‎Reason: {br_reason}
‎TP1: {tp1}  TP2: {tp2}  TP3: {tp3}
‎SL: {sl}
‎(Entry manual)\"\"\"
‎        if send_telegram_message(txt):
‎            daily_sent[symbol] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
‎            save_state()
‎        return
‎
‎    if br_side=="SELL" and (trend_down or (ema_mtf_fast and ema_mtf_fast<ema_mtf_slow)) and (rsi_htf is None or rsi_htf>=54):
‎        if daily_sent.get(symbol) == datetime.now(timezone.utc).strftime("%Y-%m-%d"): return
‎        tp1,tp2,tp3,sl = sl_tp(price_htf,"SELL")
‎        txt = f\"\"\"🔴 SELL (Break&Retest) — {LABELS[symbol]}
‎Price: {price_htf:.6f}
‎Reason: {br_reason}
‎TP1: {tp1}  TP2: {tp2}  TP3: {tp3}
‎SL: {sl}
‎(Entry manual)\"\"\"
‎        if send_telegram_message(txt):
‎            daily_sent[symbol] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
‎            save_state()
‎        return
‎
‎    # 2) Price-action + S/R + HTF/MTF confirmation
‎    buy_ok = False; sell_ok = False; reasons=[]
‎    if (touched_support or is_pin or (engulfed and e_side=="bull")):
‎        if not (trend_down):
‎            buy_ok=True
‎            if touched_support: reasons.append("S/R_touch")
‎            if is_pin: reasons.append("pinbar")
‎            if engulfed and e_side=="bull": reasons.append("engulf_bull")
‎    if (touched_res or (engulfed and e_side=="bear")):
‎        if not (trend_up):
‎            sell_ok=True
‎            if touched_res: reasons.append("S/R_touch")
‎            if engulfed and e_side=="bear": reasons.append("engulf_bear")
‎
‎    if daily_sent.get(symbol) == datetime.now(timezone.utc).strftime("%Y-%m-%d"): return
‎
‎    if buy_ok and can_send(symbol,"pa"):
‎        tp1,tp2,tp3,sl = sl_tp(price_htf,"BUY")
‎        txt = f\"\"\"🟢 BUY (PA+S/R) — {LABELS[symbol]}
‎Price: {price_htf:.6f}
‎Reasons: {', '.join(reasons)}
‎TP1:{tp1} TP2:{tp2} TP3:{tp3}
‎SL:{sl}
‎(Entry manual)\"\"\"
‎        if send_telegram_message(txt):
‎            daily_sent[symbol] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
‎            save_state()
‎        return
‎
‎    if sell_ok and can_send(symbol,"pa"):
‎        tp1,tp2,tp3,sl = sl_tp(price_htf,"SELL")
‎        txt = f\"\"\"🔴 SELL (PA+S/R) — {LABELS[symbol]}
‎Price: {price_htf:.6f}
‎Reasons: {', '.join(reasons)}
‎TP1:{tp1} TP2:{tp2} TP3:{tp3}
‎SL:{sl}
‎(Entry manual)\"\"\"
‎        if send_telegram_message(txt):
‎            daily_sent[symbol] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
‎            save_state()
‎        return
‎
‎# ---------- Worker: warmup + recurring fetch ----------
‎def warm_fetch():
‎    for s in SYMBOLS:
‎        try:
‎            htf = coinbase_klines(s, GRANULARITIES["4h"], limit=60)
‎            mtf = coinbase_klines(s, GRANULARITIES["1h"], limit=120)
‎            ltf = coinbase_klines(s, GRANULARITIES["15m"], limit=240)
‎            if htf:
‎                htf_candles[s].clear()
‎                for c in htf: htf_candles[s].append(c)
‎            if mtf:
‎                mtf_candles[s].clear()
‎                for c in mtf: mtf_candles[s].append(c)
‎            if ltf:
‎                ltf_closes[s].clear()
‎                for c in ltf: ltf_closes[s].append(c[3])
‎            print(f"warm: {s} htf:{len(htf_candles[s])} mtf:{len(mtf_candles[s])} ltf:{len(ltf_closes[s])}")
‎        except Exception as e:
‎            print("warm error", s, e)
‎
‎def worker():
‎    load_state()
‎    warm_fetch()
‎    send_telegram_message(f"🤖 Super-Pro TIDE Bot (4h/1h/15m) warming done; monitoring live.")
‎    while True:
‎        t0=time.time()
‎        for s in SYMBOLS:
‎            try:
‎                htf = coinbase_klines(s, GRANULARITIES["4h"], limit=6)
‎                mtf = coinbase_klines(s, GRANULARITIES["1h"], limit=6)
‎                ltf = coinbase_klines(s, GRANULARITIES["15m"], limit=6)
‎                if htf:
‎                    for c in htf:
‎                        if not htf_candles[s] or c[4] > htf_candles[s][-1][4]:
‎                            htf_candles[s].append(c)
‎                if mtf:
‎                    for c in mtf:
‎                        if not mtf_candles[s] or c[4] > mtf_candles[s][-1][4]:
‎                            mtf_candles[s].append(c)
‎                if ltf:
‎                    for c in ltf:
‎                        if not ltf_closes[s] or c[3] != ltf_closes[s][-1]:
‎                            ltf_closes[s].append(c[3])
‎                analyze_symbol(s)
‎                time.sleep(0.25)
‎            except Exception as e:
‎                print("worker error", s, e)
‎        save_state()
‎        elapsed=time.time()-t0
‎        # run roughly every 5 minutes
‎        time.sleep(max(60, 300 - int(elapsed)))
‎
‎# ---------- Telegram handlers (using python-telegram-bot) ----------
‎app = ApplicationBuilder().token(BOT_TOKEN).build()
‎
‎async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
‎    await context.bot.send_message(chat_id=update.effective_chat.id, text="🤖 Super-Pro TIDE Bot online. Monitoring HTF/LTF.")
‎
‎async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
‎    text = "📡 Status:\n"
‎    for s in SYMBOLS:
‎        last = ltf_closes[s][-1] if ltf_closes[s] else "no data"
‎        text += f"{LABELS[s]}: {last}\n"
‎    await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
‎
‎app.add_handler(CommandHandler("start", start_handler))
‎app.add_handler(CommandHandler("status", status_handler))
‎
‎# ---------- Flask health server for Render ----------
‎flask_app = Flask("health")
‎
‎@flask_app.route("/")
‎def health():
‎    return f"{REPO_NAME}: OK"
‎
‎# ---------- Run everything ----------
‎if __name__ == "__main__":
‎    # start Flask in background for Render healthchecks
‎    flask_thread = threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True)
‎    flask_thread.start()
‎
‎    # start worker thread
‎    worker_thread = threading.Thread(target=worker, daemon=True)
‎    worker_thread.start()
‎
‎    # start telegram polling (blocking)
‎    print("Starting Telegram polling...")
‎    app.run_polling()
‎
