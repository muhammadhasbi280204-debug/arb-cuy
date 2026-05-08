import os, time, math, requests
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import ta
import pandas as pd

load_dotenv()
client = Client(os.getenv("API_KEY"), os.getenv("API_SECRET"))
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

LEVERAGE         = 10
ORDER_USDT       = 55
ATR_SL_MULT      = 1.5
ATR_TP_MULT      = 3.0
TRAIL_TRIGGER    = 0.005
TRAIL_PCT        = 0.003
MIN_TA_VOTES     = 6
MIN_FNG          = 35
MAX_POSITIONS    = 2
SCAN_INTERVAL    = 60

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT","ETCUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT",
    "SUIUSDT","TIAUSDT","AAVEUSDT","RUNEUSDT","FILUSDT",
    "1000PEPEUSDT","WIFUSDT","JUPUSDT",
]

open_positions = {}
trade_log      = []
_last_candle   = {}

_sym_info = {}
def get_sym_info(symbol):
    if symbol in _sym_info: return _sym_info[symbol]
    try:
        for s in client.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        _sym_info[symbol] = {
                            "step": float(f["stepSize"]),
                            "minQty": float(f["minQty"])
                        }
                        return _sym_info[symbol]
    except: pass
    return {"step":1.0,"minQty":1.0}

def round_step(qty, step):
    p = max(0, int(round(-math.log(step,10),0))) if step < 1 else 0
    return round(math.floor(qty/step)*step, p)

def calc_qty(symbol, price):
    info = get_sym_info(symbol)
    return max(round_step(ORDER_USDT/price, info["step"]), info["minQty"])

def set_leverage(symbol):
    try: client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except: pass

def get_price(symbol):
    try: return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except: return 0.0

def validate_symbols():
    try:
        valid = {s["symbol"] for s in client.futures_exchange_info()["symbols"] if s["status"]=="TRADING"}
        result = list(dict.fromkeys([s for s in SYMBOLS if s in valid]))
        print(f"  ✅ {len(result)} symbols valid")
        return result
    except:
        return list(dict.fromkeys(SYMBOLS))

def get_exchange_amt(symbol, retries=3):
    """
    Coba 3x sebelum menyerah. Return None kalau semua gagal
    (bukan 0) supaya bot tidak salah anggap posisi sudah tutup.
    """
    for attempt in range(retries):
        try:
            positions = client.futures_position_information(symbol=symbol)
            for p in positions:
                amt = float(p["positionAmt"])
                if amt != 0:
                    return amt
            return 0   
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                print(f"  ⚠️  [{symbol}] Gagal query posisi ({e}) — skip cycle ini")
                return None  

def get_ohlcv(symbol, interval, limit=200):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "ct","qv","trades","tbbase","tbquote","ignore"])
        for c in ["open","high","low","close","volume","tbbase","tbquote"]:
            df[c] = df[c].astype(float)
        df["time"] = pd.to_numeric(df["time"])
        return df
    except: return None

_macro = {
    "fng":50, "fng_label":"Neutral",
    "usdt_d":5.0, "usdt_prev":5.0,
    "news":"neutral", "headlines":[],
    "last_fng":0, "last_dom":0, "last_news":0
}

def refresh_macro():
    now = time.time()
    if now - _macro["last_fng"] > 300:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1",timeout=5).json()["data"][0]
            _macro["fng"] = int(d["value"])
            _macro["fng_label"] = d["value_classification"]
            _macro["last_fng"] = now
        except: pass

    if now - _macro["last_dom"] > 300:
        try:
            d = requests.get("https://api.coingecko.com/api/v3/global",timeout=8).json()
            _macro["usdt_prev"] = _macro["usdt_d"]
            _macro["usdt_d"]    = round(d["data"]["market_cap_percentage"].get("usdt",5),2)
            _macro["last_dom"]  = now
        except: pass

    if now - _macro["last_news"] > 60:
        try:
            data = requests.get(
                "https://cryptopanic.com/api/v1/posts/?auth_token=demo&public=true&currencies=BTC",
                timeout=5).json()
            neg_kw = ["crash","hack","ban","bear","fear","lawsuit","fraud","dump",
                      "warning","collapse","scam","decline","plunge","seized","fud","sell-off"]
            pos_kw = ["bullish","rally","surge","adoption","institutional","ath",
                      "breakout","buy","approved","launched","partnership","record","soar"]
            neg = pos = 0
            hl = []
            for post in data.get("results",[])[:10]:
                t = post.get("title","")
                tl = t.lower()
                if any(w in tl for w in neg_kw): neg+=1; hl.append(f"🔴 {t[:55]}")
                elif any(w in tl for w in pos_kw): pos+=1; hl.append(f"🟢 {t[:55]}")
            _macro["news"]      = "negative" if neg>=2 else ("positive" if pos>=2 else "neutral")
            _macro["headlines"] = hl[:3]
            _macro["last_news"] = now
        except: pass

def get_regime(symbol):
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_1HOUR, 60)
    if df is None or len(df) < 55: return "RANGE"
    c     = df["close"]
    price = c.iloc[-1]
    ema20 = ta.trend.EMAIndicator(c, 20).ema_indicator().iloc[-1]
    ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator().iloc[-1]
    if ema20 > ema50 and price > ema50: return "BULL"
    if ema20 < ema50 and price < ema50: return "BEAR"
    return "RANGE"

def run_ta(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["rsi"]        = ta.momentum.RSIIndicator(c, 14).rsi()
    df["rsi_fast"]   = ta.momentum.RSIIndicator(c, 7).rsi()
    macd             = ta.trend.MACD(c)
    df["macd"]       = macd.macd()
    df["macd_sig"]   = macd.macd_signal()
    df["macd_hist"]  = macd.macd_diff()
    df["ema9"]       = ta.trend.EMAIndicator(c, 9).ema_indicator()
    df["ema21"]      = ta.trend.EMAIndicator(c, 21).ema_indicator()
    df["ema50"]      = ta.trend.EMAIndicator(c, 50).ema_indicator()
    df["ema200"]     = ta.trend.EMAIndicator(c, 200).ema_indicator()
    bb               = ta.volatility.BollingerBands(c, 20, 2)
    df["bb_hi"]      = bb.bollinger_hband()
    df["bb_lo"]      = bb.bollinger_lband()
    df["bb_mid"]     = bb.bollinger_mavg()
    df["bb_width"]   = (df["bb_hi"] - df["bb_lo"]) / df["bb_mid"]
    stoch            = ta.momentum.StochasticOscillator(h, l, c, 14, 3)
    df["stk"]        = stoch.stoch()
    df["std"]        = stoch.stoch_signal()
    df["atr"]        = ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()
    df["vol_ma"]     = v.rolling(20).mean()
    df["vol_ratio"]  = v / df["vol_ma"].replace(0,1)
    df["buy_ratio"]  = df["tbbase"] / df["volume"].replace(0,1)
    df["body"]       = abs(df["close"] - df["open"])
    df["range_"]     = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["range_"].replace(0,1)
    return df

def get_ta_votes(df, regime):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p2   = df.iloc[-3]
    long_v = short_v = 0

    rsi = last["rsi"]
    if rsi < 35:   long_v  += 1
    elif rsi > 65: short_v += 1

    rf = last["rsi_fast"]
    if rf < 30:   long_v  += 1
    elif rf > 70: short_v += 1

    if (last["macd"] > last["macd_sig"] and prev["macd"] <= prev["macd_sig"]) or \
       (prev["macd"] > prev["macd_sig"] and p2["macd"] <= p2["macd_sig"]):
        long_v += 1
    elif (last["macd"] < last["macd_sig"] and prev["macd"] >= prev["macd_sig"]) or \
         (prev["macd"] < prev["macd_sig"] and p2["macd"] >= p2["macd_sig"]):
        short_v += 1

    if last["ema9"] > last["ema21"] > last["ema50"]:   long_v  += 1
    elif last["ema9"] < last["ema21"] < last["ema50"]: short_v += 1

    if not pd.isna(last["ema200"]):
        if last["close"] > last["ema200"]: long_v  += 1
        else:                              short_v += 1

    price = last["close"]
    if price <= last["bb_lo"] * 1.002:   long_v  += 1
    elif price >= last["bb_hi"] * 0.998: short_v += 1

    k, d   = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    if k < 25 and k > d and pk <= pd_:   long_v  += 1
    elif k > 75 and k < d and pk >= pd_: short_v += 1

    br = last["buy_ratio"]
    if br > 0.60:   long_v  += 1
    elif br < 0.40: short_v += 1

    if last["vol_ratio"] > 1.2:
        if last["close"] > last["open"]: long_v  += 1
        else:                            short_v += 1

    if last["body_ratio"] > 0.6:
        if last["close"] > last["open"]: long_v  += 1
        else:                            short_v += 1

    if regime == "BULL" and short_v > long_v:   return "NONE", long_v, short_v
    if regime == "BEAR" and long_v  > short_v:  return "NONE", long_v, short_v
    if regime == "RANGE" and max(long_v,short_v) < 7: return "NONE", long_v, short_v

    if long_v >= MIN_TA_VOTES and long_v > short_v + 1:
        return "LONG", long_v, short_v
    if short_v >= MIN_TA_VOTES and short_v > long_v + 1:
        return "SHORT", long_v, short_v
    return "NONE", long_v, short_v

def get_ob_imbalance(symbol):
    try:
        ob    = client.futures_order_book(symbol=symbol, limit=50)
        bids  = sum(float(b[1]) for b in ob["bids"])
        asks  = sum(float(a[1]) for a in ob["asks"])
        total = bids + asks
        return round((bids-asks)/total, 3) if total else 0.0
    except: return 0.0

def get_cum_delta(df, lookback=10):
    if len(df) < lookback: return 0.0
    recent = df.tail(lookback).copy()
    recent = recent.copy()
    recent["delta"] = recent["tbbase"] - (recent["volume"] - recent["tbbase"])
    cum  = recent["delta"].sum()
    norm = cum / (recent["volume"].sum() + 1)
    return round(norm, 3)

def detect_whale(df):
    last   = df.iloc[-1]
    vol_ma = df["vol_ma"].iloc[-1]
    if pd.isna(vol_ma) or vol_ma == 0: return "none", 1.0
    ratio = last["volume"] / vol_ma
    if ratio >= 3.5:
        return ("buy_whale" if last["close"] > last["open"] else "sell_whale"), ratio
    elif ratio >= 2.0:
        return ("mild_buy" if last["close"] > last["open"] else "mild_sell"), ratio
    return "none", ratio

def get_funding(symbol):
    try:
        data = client.futures_funding_rate(symbol=symbol, limit=1)
        return round(float(data[0]["fundingRate"])*100, 4)
    except: return 0.0

def should_enter(symbol, df):
    info = {}

    fng     = _macro["fng"]
    news    = _macro["news"]
    usdt_up = _macro["usdt_d"] > _macro["usdt_prev"]

    if fng < MIN_FNG:
        return None, 0, 0, {"skip":"Fear terlalu tinggi"}
    if news == "negative":
        return None, 0, 0, {"skip":"News buruk"}
    if usdt_up:
        return None, 0, 0, {"skip":"USDT.D naik (risk-off)"}

    regime = get_regime(symbol)
    info["regime"] = regime

    prev_candle_time = int(df["time"].iloc[-2])
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 60:
        return None, 0, 0, {"skip":"Data tidak cukup"}

    if _last_candle.get(symbol) == prev_candle_time:
        return None, 0, 0, {"skip":"Sudah dianalisa di candle ini"}

    df_closed = run_ta(df_closed)
    ta_dir, lv, sv = get_ta_votes(df_closed, regime)
    info["ta"] = f"{ta_dir} L:{lv}/S:{sv}"
    if ta_dir == "NONE":
        return None, 0, 0, {"skip":f"TA tidak konfirmasi (L:{lv} S:{sv})"}

    ob_imb = get_ob_imbalance(symbol)
    info["ob"] = ob_imb
    if ta_dir == "LONG"  and ob_imb < -0.1:
        return None, 0, 0, {"skip":f"OB melawan LONG"}
    if ta_dir == "SHORT" and ob_imb > 0.1:
        return None, 0, 0, {"skip":f"OB melawan SHORT"}

    cum_d = get_cum_delta(df_closed)
    info["cum_delta"] = cum_d
    if ta_dir == "LONG"  and cum_d < -0.15:
        return None, 0, 0, {"skip":f"CumDelta bearish"}
    if ta_dir == "SHORT" and cum_d > 0.15:
        return None, 0, 0, {"skip":f"CumDelta bullish"}

    whale_dir, whale_ratio = detect_whale(df_closed)
    info["whale"] = f"{whale_dir}({whale_ratio:.1f}x)"
    if ta_dir == "LONG"  and whale_dir == "sell_whale":
        return None, 0, 0, {"skip":"Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, {"skip":"Whale buy aktif"}

    funding = get_funding(symbol)
    info["funding"] = funding
    if ta_dir == "LONG"  and funding >  0.1:
        return None, 0, 0, {"skip":f"Funding terlalu positif"}
    if ta_dir == "SHORT" and funding < -0.1:
        return None, 0, 0, {"skip":f"Funding terlalu negatif"}

    bb_width = df_closed["bb_width"].iloc[-1]
    info["bb_width"] = round(bb_width, 4)
    if bb_width < 0.01:
        return None, 0, 0, {"skip":f"BB terlalu sempit, market choppy"}

    atr   = df_closed["atr"].iloc[-1]
    price = df_closed["close"].iloc[-1]
    if ta_dir == "LONG":
        sl_price = round(price - ATR_SL_MULT * atr, 8)
        tp_price = round(price + ATR_TP_MULT * atr, 8)
    else:
        sl_price = round(price + ATR_SL_MULT * atr, 8)
        tp_price = round(price - ATR_TP_MULT * atr, 8)

    sl_pct = abs(price - sl_price) / price
    if sl_pct > 0.03:
        return None, 0, 0, {"skip":f"ATR terlalu besar (SL={sl_pct*100:.1f}%)"}

    _last_candle[symbol] = prev_candle_time
    info["atr_sl_pct"] = f"{sl_pct*100:.2f}%"
    return ta_dir, sl_price, tp_price, info

def open_trade(symbol, side, sl_price, tp_price, info):
    try:
        set_leverage(symbol)
        price = get_price(symbol)
        qty   = calc_qty(symbol, price)
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side=="LONG" else SIDE_SELL,
            type=ORDER_TYPE_MARKET, quantity=qty)
        entry    = get_price(symbol)
        trail_sl = entry*(1-TRAIL_PCT) if side=="LONG" else entry*(1+TRAIL_PCT)
        open_positions[symbol] = {
            "side":side, "entry":entry, "qty":qty,
            "sl":sl_price, "tp":tp_price,
            "peak":entry, "trail_sl":trail_sl,
            "trailing_active": False
        }
        sl_pct = abs(entry-sl_price)/entry*100
        tp_pct = abs(tp_price-entry)/entry*100
        print(f"  ✅ [{symbol}] {side} @{entry:.5f} qty={qty}")
        print(f"     SL:{sl_price:.5f}(-{sl_pct:.2f}%) TP:{tp_price:.5f}(+{tp_pct:.2f}%)")
        print(f"     regime:{info.get('regime','?')} TA:{info.get('ta','?')} OB:{info.get('ob',0):+.2f} Δ:{info.get('cum_delta',0):+.3f} 🐋:{info.get('whale','?')}")
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal entry: {e}")

def close_trade(symbol, reason=""):
    """
    Close posisi. Kalau exchange query gagal, tetap catat di log
    tapi jangan hapus dari open_positions supaya dicoba lagi.
    """
    try:
        amt = get_exchange_amt(symbol)

        if amt is None:
            print(f"  ⚠️  [{symbol}] Tidak bisa query exchange, tunda close")
            return False

        if amt == 0:
            if symbol in open_positions:
                pos = open_positions[symbol]
                exit_ = get_price(symbol)
                if exit_ > 0:
                    pnl = (exit_-pos["entry"])*pos["qty"] if pos["side"]=="LONG" \
                          else (pos["entry"]-exit_)*pos["qty"]
                    pct = pnl/(pos["entry"]*pos["qty"])*100
                    emoji = "🟢" if pnl>=0 else "🔴"
                    print(f"  ⚠️  [{symbol}] Posisi sudah tutup di exchange (liquidasi/manual?)")
                    print(f"     {emoji} Est P&L: {pnl:+.4f} USDT ({pct:+.2f}%)")
                    trade_log.append({"symbol":symbol,"side":pos["side"],
                                      "pnl":round(pnl,4),"reason":"External close"})
                open_positions.pop(symbol, None)
            return True

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if amt>0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=abs(amt), reduceOnly=True)

        if symbol in open_positions:
            pos   = open_positions[symbol]
            exit_ = get_price(symbol)
            pnl   = (exit_-pos["entry"])*pos["qty"] if pos["side"]=="LONG" \
                    else (pos["entry"]-exit_)*pos["qty"]
            pct   = pnl/(pos["entry"]*pos["qty"])*100
            emoji = "🟢" if pnl>=0 else "🔴"
            print(f"  💰 [{symbol}] CLOSED — {reason}")
            print(f"     {emoji} P&L: {pnl:+.4f} USDT ({pct:+.2f}%)")
            trade_log.append({"symbol":symbol,"side":pos["side"],
                              "pnl":round(pnl,4),"reason":reason})
        open_positions.pop(symbol, None)
        return True

    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal close: {e} — akan dicoba lagi")
        return False

def manage_positions():
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_price(symbol)

        if price == 0:
            print(f"  ⚠️  [{symbol}] Tidak bisa get price, skip cycle ini")
            continue

        entry = pos["entry"]
        side  = pos["side"]

        if _macro["news"] == "negative":
            close_trade(symbol, "📰 Emergency — bad news")
            continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            if profit_pct >= TRAIL_TRIGGER and not pos["trailing_active"]:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @ {price:.5f} (profit {profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 - TRAIL_PCT)

            if price >= pos["tp"]:
                close_trade(symbol, f"✨ TAKE PROFIT"); continue
            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, f"🔄 Trailing Stop (locked profit)"); continue
            if price <= pos["sl"]:
                close_trade(symbol, f"🛑 STOP LOSS"); continue

            pnl_now = (price - entry) * pos["qty"]
            trail_info = f" | Trail SL:{pos['trail_sl']:.5f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] LONG @{entry:.5f} now:{price:.5f} | P&L:{pnl_now:+.4f}{trail_info}")

        else:  
            profit_pct = (entry - price) / entry

            if profit_pct >= TRAIL_TRIGGER and not pos["trailing_active"]:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @ {price:.5f} (profit {profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 + TRAIL_PCT)

            if price <= pos["tp"]:
                close_trade(symbol, f"✨ TAKE PROFIT"); continue
            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, f"🔄 Trailing Stop (locked profit)"); continue
            if price >= pos["sl"]:
                close_trade(symbol, f"🛑 STOP LOSS"); continue

            pnl_now = (entry - price) * pos["qty"]
            trail_info = f" | Trail SL:{pos['trail_sl']:.5f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] SHORT @{entry:.5f} now:{price:.5f} | P&L:{pnl_now:+.4f}{trail_info}")

def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"]>0)
    n     = len(trade_log)
    wr    = wins/n*100 if n else 0
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% W:{wins} L:{n-wins} | P&L:{total:+.4f} USDT")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"]>0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f} USDT — {t['reason'][:35]}")

def run_bot():
    print("🤖 Bot v7 Fix — Quality Over Quantity")
    print(f"   Leverage   : {LEVERAGE}x | Order: ${ORDER_USDT} USDT margin")
    print(f"   SL/TP      : {ATR_SL_MULT}x/{ATR_TP_MULT}x ATR (RR 1:2, dinamis)")
    print(f"   Trailing   : aktif setelah +{TRAIL_TRIGGER*100}% profit")
    print(f"   Min Votes  : {MIN_TA_VOTES}/10 | Min F&G: {MIN_FNG}")
    print(f"   Max Posisi : {MAX_POSITIONS}\n")

    print("  ⏳ Setup...")
    symbols = validate_symbols()
    for s in symbols: get_sym_info(s)
    refresh_macro()
    print(f"  ✅ {len(symbols)} symbols | F&G:{_macro['fng']} | News:{_macro['news']}\n")

    cycle = 0
    while True:
        cycle += 1
        refresh_macro()
        manage_positions()   

        print(f"\n{'='*66}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | USDT:{_macro['usdt_d']}% | News:{_macro['news']}")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*66}")

        skipped    = 0
        candidates = []

        if len(open_positions) < MAX_POSITIONS and _macro["news"] != "negative":
            for symbol in symbols:
                if symbol in open_positions: continue
                df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 220)
                if df is None or len(df) < 70: continue

                side, sl, tp, info = should_enter(symbol, df)
                if side:
                    candidates.append((symbol, side, sl, tp, info))
                else:
                    skipped += 1

            if candidates:
                candidates.sort(key=lambda x: abs(x[4].get("ob",0)), reverse=True)
                print(f"\n  🎯 {len(candidates)} setup valid | {skipped} di-skip")
                for sym, side, sl, tp, info in candidates[:3]:
                    print(f"     ⭐ {sym} {side} | {info.get('ta','?')} | OB:{info.get('ob',0):+.2f} | regime:{info.get('regime','?')}")
                for sym, side, sl, tp, info in candidates:
                    if len(open_positions) >= MAX_POSITIONS: break
                    open_trade(sym, side, sl, tp, info)
            else:
                print(f"  ⏳ {skipped} coins di-scan, belum ada setup valid")
        else:
            print(f"  ⏸️  Posisi penuh atau kondisi tidak aman")

        print_summary()
        print(f"\n  ⏱️  {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
