"""
Bot Trading v9 — Enhanced Entry + Smarter Exit
===============================================
Upgrade dari v8:

ENTRY FILTER (baru/ditingkatkan):
- S/R Check: deteksi swing high/low dari 4H candle, skip entry kalau harga
  terlalu dekat resistance (LONG) atau support (SHORT)
- Composite Score: setiap sinyal diberi bobot berbeda, bukan sekedar vote count
  MACD histogram > RSI > EMA stack > Volume > OB imbalance > Funding
- Volume Spike Ketat: entry hanya kalau ada lonjakan volume 1.5x di 15m terakhir
  SEKALIGUS taker buy ratio mendukung arah trade

MACRO FILTER (ditingkatkan):
- BTC momentum 1H + 4H harus searah (double timeframe confirmation)
- USDT.D threshold lebih ketat: naik 0.03% sudah dianggap risk-off
- F&G: kalau di zona ekstrem (>85 greedy), skip LONG; kalau <20, skip semua
- Global crypto market cap change: kalau mcap turun >2% dalam 1 jam, skip LONG
- News sentiment lebih granular: 3 level (strong_neg / neg / neutral / pos)

EXIT STRATEGY (baru):
- Partial TP: tutup 50% posisi di TP1 (2x ATR), biarkan 50% lanjut ke TP2 (4x ATR)
- Break-even stop: setelah TP1 tercapai, SL sisa posisi pindah ke entry price
- Trailing stop pada sisa 50%: aktif setelah profit +0.5%, trail 0.3%
- Emergency exit: kalau BTC trend balik mendadak, tutup 100% langsung
"""

import os, time, math, json, requests
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import ta
import pandas as pd

load_dotenv()
client = Client(os.getenv("API_KEY"), os.getenv("API_SECRET"))
# Hapus baris berikut untuk akun REAL:
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

# ════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════
LEVERAGE              = 10
ORDER_USDT            = 55
ATR_SL_MULT           = 2.0
ATR_TP1_MULT          = 2.0     # TP1 (50% tutup) = 2x ATR → RR 1:1
ATR_TP2_MULT          = 4.0     # TP2 (50% sisanya) = 4x ATR → RR 1:2
TRAIL_TRIGGER         = 0.005   # aktifkan trailing setelah +0.5%
TRAIL_PCT             = 0.003
MIN_COMPOSITE_SCORE   = 62      # skor minimum untuk entry (0-100)
MIN_FNG               = 45
MAX_FNG_LONG          = 85      # kalau greedy ekstrem, skip LONG (euphoria risk)
MIN_FNG_ANY           = 20      # kalau fear ekstrem, skip semua
MAX_POSITIONS         = 2
SCAN_INTERVAL         = 60
MAX_CONSEC_LOSS       = 2
MIN_MARKET_BREADTH    = 0.45
MIN_VOLUME_SPIKE      = 1.5     # minimal 1.5x volume MA untuk konfirmasi entry
SR_BUFFER             = 0.008   # 0.8% buffer dari S/R level

# USDT.D threshold lebih ketat dari v8
USDT_RISK_OFF_DELTA   = 0.03    # kalau naik 0.03% sudah risk-off (was 0.05%)

# Smart Cooldown
COOLDOWN_BTC_BAD      = {"BEAR", "MILD_BEAR"}
COOLDOWN_BREADTH_MAX  = 0.40
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL"}
COOLDOWN_BREADTH_MIN  = 0.50

# Composite score weights (total harus 100)
SCORE_WEIGHTS = {
    "macd_hist":    18,   # momentum paling penting
    "rsi":          15,   # overbought/oversold
    "ema_stack":    14,   # trend alignment
    "volume":       13,   # konfirmasi dengan volume
    "ob_imbalance": 12,   # order book
    "cum_delta":    10,   # taker flow
    "stoch":         8,   # momentum oscillator
    "bb":            6,   # price vs band
    "funding":       4,   # market positioning
}

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT","ETCUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT",
    "SUIUSDT","TIAUSDT","AAVEUSDT","RUNEUSDT","FILUSDT",
    "1000PEPEUSDT","WIFUSDT","JUPUSDT",
]

open_positions  = {}
trade_log       = []
_last_candle    = {}
_consec_loss    = 0
_in_cooldown    = False

# ════════════════════════════════════════════════════
#  UTILS
# ════════════════════════════════════════════════════
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
    return {"step": 1.0, "minQty": 1.0}

def round_step(qty, step):
    p = max(0, int(round(-math.log(step, 10), 0))) if step < 1 else 0
    return round(math.floor(qty / step) * step, p)

def calc_qty(symbol, price, fraction=1.0):
    """Hitung qty. fraction=0.5 untuk partial close."""
    info = get_sym_info(symbol)
    return max(round_step((ORDER_USDT * fraction) / price, info["step"]), info["minQty"])

def set_leverage(symbol):
    try: client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except: pass

def get_price(symbol):
    try: return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except: return 0.0

def validate_symbols():
    try:
        valid  = {s["symbol"] for s in client.futures_exchange_info()["symbols"] if s["status"] == "TRADING"}
        result = list(dict.fromkeys([s for s in SYMBOLS if s in valid]))
        print(f"  ✅ {len(result)} symbols valid")
        return result
    except:
        return list(dict.fromkeys(SYMBOLS))

def get_exchange_amt(symbol, retries=3):
    for attempt in range(retries):
        try:
            for p in client.futures_position_information(symbol=symbol):
                amt = float(p["positionAmt"])
                if amt != 0: return amt
            return 0
        except Exception as e:
            if attempt < retries - 1: time.sleep(1)
            else:
                print(f"  ⚠️  [{symbol}] Gagal query posisi — skip")
                return None

# ════════════════════════════════════════════════════
#  OHLCV
# ════════════════════════════════════════════════════
def get_ohlcv(symbol, interval, limit=200):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "time", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "tbbase", "tbquote", "ignore"])
        for c in ["open", "high", "low", "close", "volume", "tbbase", "tbquote"]:
            df[c] = df[c].astype(float)
        df["time"] = pd.to_numeric(df["time"])
        return df
    except: return None

# ════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE (4H Swing Points)
# ════════════════════════════════════════════════════
def get_sr_levels(symbol, lookback=30):
    """
    Hitung swing high/low dari 4H candle sebagai S/R level.
    Returns dict: {"resistance": [...], "support": [...]}
    Swing high = high yang lebih tinggi dari 2 candle kiri dan 2 kanan.
    Swing low  = low yang lebih rendah dari 2 candle kiri dan 2 kanan.
    """
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_4HOUR, lookback + 5)
    if df is None or len(df) < 10:
        return {"resistance": [], "support": []}

    highs = df["high"].values
    lows  = df["low"].values
    resistance = []
    support    = []

    # Cari swing points (skip 2 candle pertama dan terakhir)
    for i in range(2, len(highs) - 2):
        # Swing high: lebih tinggi dari 2 tetangga di kiri & kanan
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance.append(highs[i])
        # Swing low
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support.append(lows[i])

    return {"resistance": sorted(resistance, reverse=True)[:5],
            "support":    sorted(support)[:5]}

def check_sr_clear(symbol, price, direction):
    """
    Cek apakah harga terlalu dekat dengan S/R yang menghalangi trade.
    - LONG: harga tidak boleh < SR_BUFFER dari resistance terdekat
    - SHORT: harga tidak boleh > SR_BUFFER dari support terdekat
    Returns (ok: bool, reason: str)
    """
    sr = get_sr_levels(symbol)

    if direction == "LONG":
        # Cari resistance terdekat di atas harga
        nearby_res = [r for r in sr["resistance"] if r > price]
        if nearby_res:
            nearest = min(nearby_res)
            gap_pct  = (nearest - price) / price
            if gap_pct < SR_BUFFER:
                return False, f"Terlalu dekat resistance {nearest:.4f} (gap {gap_pct*100:.2f}%)"
        # Cek juga: harga tidak boleh di bawah support yang harusnya jadi TP
        # (berarti kita entry di posisi yang tidak bagus)

    elif direction == "SHORT":
        # Cari support terdekat di bawah harga
        nearby_sup = [s for s in sr["support"] if s < price]
        if nearby_sup:
            nearest = max(nearby_sup)
            gap_pct  = (price - nearest) / price
            if gap_pct < SR_BUFFER:
                return False, f"Terlalu dekat support {nearest:.4f} (gap {gap_pct*100:.2f}%)"

    return True, ""

# ════════════════════════════════════════════════════
#  MACRO CACHE
# ════════════════════════════════════════════════════
_macro = {
    "fng": 50, "fng_label": "Neutral",
    "usdt_d": 5.0, "usdt_prev": 5.0,
    "news": "neutral", "news_strength": 0, "headlines": [],
    "btc_trend_15m": "UNKNOWN",
    "btc_trend_1h":  "UNKNOWN",
    "btc_trend_4h":  "UNKNOWN",
    "market_breadth": 0.5,
    "global_mcap_chg": 0.0,   # % change global market cap
    "last_fng": 0, "last_dom": 0, "last_news": 0,
    "last_btc": 0, "last_breadth": 0, "last_mcap": 0
}

def _calc_btc_trend(df):
    """Hitung trend BTC dari dataframe OHLCV."""
    if df is None or len(df) < 30:
        return "UNKNOWN"
    c     = df["close"]
    price = c.iloc[-1]
    ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
    ema21 = ta.trend.EMAIndicator(c, 21).ema_indicator().iloc[-1]
    ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator().iloc[-1]
    chg   = (price - c.iloc[-4]) / c.iloc[-4] * 100  # ~1 jam di 15m

    if price > ema9 > ema21 > ema50 and chg > 0:
        return "BULL"
    elif price < ema9 < ema21 < ema50 and chg < 0:
        return "BEAR"
    elif price > ema21 and chg > -0.3:
        return "MILD_BULL"
    elif price < ema21 and chg < 0.3:
        return "MILD_BEAR"
    return "SIDEWAYS"

def refresh_macro():
    now = time.time()

    # Fear & Greed (tiap 5 menit)
    if now - _macro["last_fng"] > 300:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()["data"][0]
            _macro["fng"]       = int(d["value"])
            _macro["fng_label"] = d["value_classification"]
            _macro["last_fng"]  = now
        except: pass

    # USDT Dominance (tiap 5 menit)
    if now - _macro["last_dom"] > 300:
        try:
            d = requests.get("https://api.coingecko.com/api/v3/global", timeout=8).json()
            _macro["usdt_prev"] = _macro["usdt_d"]
            _macro["usdt_d"]    = round(d["data"]["market_cap_percentage"].get("usdt", 5), 2)
            # Global mcap change
            chg_pct = d["data"].get("market_cap_change_percentage_24h_usd", 0)
            _macro["global_mcap_chg"] = round(chg_pct, 2)
            _macro["last_dom"]  = now
        except: pass

    # News (tiap 60 detik) — lebih granular
    if now - _macro["last_news"] > 60:
        try:
            data = requests.get(
                "https://cryptopanic.com/api/v1/posts/?auth_token=demo&public=true&currencies=BTC",
                timeout=5).json()
            neg_kw_strong = ["crash","hack","ban","fraud","collapse","seized","scam"]
            neg_kw_mild   = ["bear","fear","lawsuit","dump","warning","plunge","fud","sell-off","decline"]
            pos_kw_strong = ["institutional","ath","approved","record","bullish","rally","surge"]
            pos_kw_mild   = ["adoption","breakout","buy","launched","partnership","soar"]
            neg = pos = 0
            hl  = []
            for post in data.get("results", [])[:10]:
                t  = post.get("title", "")
                tl = t.lower()
                if any(w in tl for w in neg_kw_strong): neg += 2; hl.append(f"🔴🔴 {t[:55]}")
                elif any(w in tl for w in neg_kw_mild): neg += 1; hl.append(f"🔴 {t[:55]}")
                elif any(w in tl for w in pos_kw_strong): pos += 2; hl.append(f"🟢🟢 {t[:55]}")
                elif any(w in tl for w in pos_kw_mild):   pos += 1; hl.append(f"🟢 {t[:55]}")

            score = pos - neg
            if score <= -4:   sentiment = "strong_negative"
            elif score <= -2: sentiment = "negative"
            elif score >= 4:  sentiment = "strong_positive"
            elif score >= 2:  sentiment = "positive"
            else:             sentiment = "neutral"

            _macro["news"]          = sentiment
            _macro["news_strength"] = score
            _macro["headlines"]     = hl[:3]
            _macro["last_news"]     = now
        except: pass

    # BTC Trend — multi timeframe (tiap 60 detik)
    if now - _macro["last_btc"] > 60:
        try:
            df_15m = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_15MINUTE, 60)
            df_1h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_1HOUR, 60)
            df_4h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_4HOUR, 60)
            _macro["btc_trend_15m"] = _calc_btc_trend(df_15m)
            _macro["btc_trend_1h"]  = _calc_btc_trend(df_1h)
            _macro["btc_trend_4h"]  = _calc_btc_trend(df_4h)
            _macro["last_btc"]      = now
        except: pass

    # Market Breadth (tiap 5 menit)
    if now - _macro["last_breadth"] > 300:
        try:
            bullish = 0
            sample  = SYMBOLS[:15]
            for sym in sample:
                df = get_ohlcv(sym, Client.KLINE_INTERVAL_15MINUTE, 10)
                if df is not None and len(df) >= 5:
                    c    = df["close"]
                    ema9 = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
                    if c.iloc[-1] > ema9 and df["close"].iloc[-1] > df["open"].iloc[-1]:
                        bullish += 1
            _macro["market_breadth"] = bullish / len(sample)
            _macro["last_breadth"]   = now
        except: pass

# ════════════════════════════════════════════════════
#  HELPER: BTC trend gabungan
# ════════════════════════════════════════════════════
BULL_TRENDS = {"BULL", "MILD_BULL"}
BEAR_TRENDS = {"BEAR", "MILD_BEAR"}

def btc_multi_tf_ok_for(direction):
    """
    Cek apakah BTC trend di 15m + 1H + 4H mendukung direction.
    Aturan:
    - LONG: minimal 1H dan 4H tidak BEAR/MILD_BEAR (keduanya harus tidak bearish)
            15m boleh sideways asal 1H dan 4H bullish/sideways
    - SHORT: minimal 1H dan 4H tidak BULL/MILD_BULL
    Returns (ok, reason)
    """
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        if t4h in BEAR_TRENDS:
            return False, f"BTC 4H={t4h} bearish — skip LONG"
        if t1h in BEAR_TRENDS:
            return False, f"BTC 1H={t1h} bearish — skip LONG"
        # Extra ketat: kalau 1H dan 4H keduanya sideways, tapi 15m bear → skip
        if t1h == "SIDEWAYS" and t15 in BEAR_TRENDS:
            return False, f"BTC 1H=SIDEWAYS + 15m={t15} — skip LONG"
    elif direction == "SHORT":
        if t4h in BULL_TRENDS:
            return False, f"BTC 4H={t4h} bullish — skip SHORT"
        if t1h in BULL_TRENDS:
            return False, f"BTC 1H={t1h} bullish — skip SHORT"
        if t1h == "SIDEWAYS" and t15 in BULL_TRENDS:
            return False, f"BTC 1H=SIDEWAYS + 15m={t15} — skip SHORT"

    return True, ""

# ════════════════════════════════════════════════════
#  SMART COOLDOWN
# ════════════════════════════════════════════════════
def check_cooldown_recover():
    btc_ok     = _macro["btc_trend_15m"] in COOLDOWN_BTC_RECOVER
    breadth_ok = _macro["market_breadth"] >= COOLDOWN_BREADTH_MIN
    return btc_ok and breadth_ok

def is_cooldown_active():
    global _in_cooldown
    if not _in_cooldown:
        return False
    if check_cooldown_recover():
        _in_cooldown = False
        print(f"  ✅ Cooldown dibatalkan! BTC:{_macro['btc_trend_15m']} "
              f"Breadth:{_macro['market_breadth']*100:.0f}%")
        return False
    return True

def cooldown_reason():
    reasons = []
    if _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD:
        reasons.append(f"BTC {_macro['btc_trend_15m']}")
    if _macro["market_breadth"] < COOLDOWN_BREADTH_MIN:
        reasons.append(f"breadth {_macro['market_breadth']*100:.0f}%")
    return " & ".join(reasons) if reasons else "kondisi belum jelas"

# ════════════════════════════════════════════════════
#  REGIME (1H per symbol)
# ════════════════════════════════════════════════════
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

# ════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS (15m)
# ════════════════════════════════════════════════════
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
    df["vol_ratio"]  = v / df["vol_ma"].replace(0, 1)
    df["buy_ratio"]  = df["tbbase"] / df["volume"].replace(0, 1)
    df["body"]       = abs(df["close"] - df["open"])
    df["range_"]     = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["range_"].replace(0, 1)
    return df

def calc_composite_score(df, regime, ob_imb, cum_d, funding):
    """
    Hitung composite score (0-100) berdasarkan bobot per sinyal.
    Returns: (direction: "LONG"/"SHORT"/"NONE", score: float, breakdown: dict)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]
    p2   = df.iloc[-3]
    W    = SCORE_WEIGHTS
    breakdown = {}
    long_score = short_score = 0.0

    # 1. MACD Histogram momentum (bobot: 18)
    hist_now  = last["macd_hist"]
    hist_prev = prev["macd_hist"]
    # Accelerating → lebih kuat
    if hist_now > 0 and hist_now > hist_prev:
        pts = W["macd_hist"] if (last["macd"] > last["macd_sig"] and prev["macd"] <= prev["macd_sig"]) \
              else W["macd_hist"] * 0.7
        long_score += pts
        breakdown["macd"] = f"+{pts:.1f}L"
    elif hist_now < 0 and hist_now < hist_prev:
        pts = W["macd_hist"] if (last["macd"] < last["macd_sig"] and prev["macd"] >= prev["macd_sig"]) \
              else W["macd_hist"] * 0.7
        short_score += pts
        breakdown["macd"] = f"+{pts:.1f}S"
    else:
        breakdown["macd"] = "0"

    # 2. RSI (bobot: 15)
    rsi = last["rsi"]
    if rsi < 35:
        pts = W["rsi"] if rsi < 30 else W["rsi"] * 0.6
        long_score += pts; breakdown["rsi"] = f"+{pts:.1f}L"
    elif rsi > 65:
        pts = W["rsi"] if rsi > 70 else W["rsi"] * 0.6
        short_score += pts; breakdown["rsi"] = f"+{pts:.1f}S"
    else:
        breakdown["rsi"] = "0"

    # 3. EMA Stack (bobot: 14)
    e9, e21, e50 = last["ema9"], last["ema21"], last["ema50"]
    if e9 > e21 > e50:
        long_score += W["ema_stack"]; breakdown["ema"] = f"+{W['ema_stack']}L"
    elif e9 < e21 < e50:
        short_score += W["ema_stack"]; breakdown["ema"] = f"+{W['ema_stack']}S"
    else:
        # Partial: hanya 2 dari 3 EMA aligned
        if e9 > e21: long_score += W["ema_stack"] * 0.4
        elif e9 < e21: short_score += W["ema_stack"] * 0.4
        breakdown["ema"] = "partial"

    # 4. Volume + taker ratio (bobot: 13)
    vr = last["vol_ratio"]
    br = last["buy_ratio"]
    if vr >= MIN_VOLUME_SPIKE:
        if last["close"] > last["open"] and br > 0.55:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 1.5)
            long_score += pts; breakdown["vol"] = f"+{pts:.1f}L({vr:.1f}x)"
        elif last["close"] < last["open"] and br < 0.45:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 1.5)
            short_score += pts; breakdown["vol"] = f"+{pts:.1f}S({vr:.1f}x)"
        else:
            breakdown["vol"] = f"spike({vr:.1f}x) tapi arah ambigu"
    else:
        breakdown["vol"] = f"no spike({vr:.1f}x)"

    # 5. Order Book Imbalance (bobot: 12)
    if ob_imb > 0.15:
        pts = W["ob_imbalance"] * min(ob_imb / 0.15, 1.5)
        long_score += pts; breakdown["ob"] = f"+{pts:.1f}L({ob_imb:+.2f})"
    elif ob_imb < -0.15:
        pts = W["ob_imbalance"] * min(abs(ob_imb) / 0.15, 1.5)
        short_score += pts; breakdown["ob"] = f"+{pts:.1f}S({ob_imb:+.2f})"
    else:
        breakdown["ob"] = f"neutral({ob_imb:+.2f})"

    # 6. Cumulative Delta (bobot: 10)
    if cum_d > 0.15:
        long_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}L"
    elif cum_d < -0.15:
        short_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}S"
    else:
        breakdown["delta"] = "0"

    # 7. Stochastic (bobot: 8)
    k, d   = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    if k < 25 and k > d and pk <= pd_:
        long_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}L"
    elif k > 75 and k < d and pk >= pd_:
        short_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}S"
    else:
        breakdown["stoch"] = "0"

    # 8. Bollinger Band (bobot: 6)
    price = last["close"]
    if price <= last["bb_lo"] * 1.002:
        long_score += W["bb"]; breakdown["bb"] = f"+{W['bb']}L"
    elif price >= last["bb_hi"] * 0.998:
        short_score += W["bb"]; breakdown["bb"] = f"+{W['bb']}S"
    else:
        breakdown["bb"] = "0"

    # 9. Funding Rate (bobot: 4)
    # Funding positif tinggi → overkrowded LONG → short squeeze risk
    if funding < -0.05:    # funding negatif → pasar short, bisa squeeze ke LONG
        long_score += W["funding"]; breakdown["funding"] = f"+{W['funding']}L"
    elif funding > 0.05:   # funding positif → pasar long, bisa squeeze ke SHORT
        short_score += W["funding"]; breakdown["funding"] = f"+{W['funding']}S"
    else:
        breakdown["funding"] = "0"

    # Regime penalty/bonus
    if regime == "BULL":
        long_score  *= 1.1   # bonus di bull regime
        short_score *= 0.8   # penalti short di bull regime
    elif regime == "BEAR":
        short_score *= 1.1
        long_score  *= 0.8
    # RANGE: tidak ada penyesuaian — butuh skor lebih tinggi untuk masuk

    # Normalisasi ke 0-100
    max_possible = sum(W.values()) * 1.5  # account for multiplier
    long_pct  = min(long_score  / max_possible * 100, 100)
    short_pct = min(short_score / max_possible * 100, 100)

    if long_pct >= MIN_COMPOSITE_SCORE and long_pct > short_pct + 10:
        return "LONG", long_pct, breakdown
    if short_pct >= MIN_COMPOSITE_SCORE and short_pct > long_pct + 10:
        return "SHORT", short_pct, breakdown
    return "NONE", max(long_pct, short_pct), breakdown

def check_volume_spike_confirm(df, direction):
    """
    Volume spike WAJIB ada di 3 candle terakhir untuk konfirmasi entry.
    Lebih ketat dari v8 yang cuma vote.
    """
    recent = df.iloc[-3:]
    for _, row in recent.iterrows():
        vr = row["vol_ratio"]
        br = row["buy_ratio"]
        if vr >= MIN_VOLUME_SPIKE:
            if direction == "LONG" and row["close"] > row["open"] and br > 0.52:
                return True, f"vol {vr:.1f}x buy {br:.2f}"
            if direction == "SHORT" and row["close"] < row["open"] and br < 0.48:
                return True, f"vol {vr:.1f}x sell {1-br:.2f}"
    return False, "no volume spike"

def get_ob_imbalance(symbol):
    try:
        ob    = client.futures_order_book(symbol=symbol, limit=50)
        bids  = sum(float(b[1]) for b in ob["bids"])
        asks  = sum(float(a[1]) for a in ob["asks"])
        total = bids + asks
        return round((bids - asks) / total, 3) if total else 0.0
    except: return 0.0

def get_cum_delta(df, lookback=10):
    if len(df) < lookback: return 0.0
    recent = df.tail(lookback).copy()
    recent["delta"] = recent["tbbase"] - (recent["volume"] - recent["tbbase"])
    norm = recent["delta"].sum() / (recent["volume"].sum() + 1)
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
        return round(float(data[0]["fundingRate"]) * 100, 4)
    except: return 0.0

# ════════════════════════════════════════════════════
#  MASTER ENTRY FILTER
# ════════════════════════════════════════════════════
def should_enter(symbol, df):
    info = {}

    # ── 1. Smart Cooldown ─────────────────────────────────────
    if is_cooldown_active():
        return None, 0, 0, 0, {"skip": f"🧊 Cooldown ({cooldown_reason()})"}

    # ── 2. Macro hard blocks ──────────────────────────────────
    fng      = _macro["fng"]
    news     = _macro["news"]
    usdt_up  = _macro["usdt_d"] > _macro["usdt_prev"] + USDT_RISK_OFF_DELTA
    mcap_bad = _macro["global_mcap_chg"] < -2.0  # mcap turun >2% dalam 24h

    if fng < MIN_FNG_ANY:
        return None, 0, 0, 0, {"skip": f"F&G ekstrem rendah ({fng}) — skip semua"}
    if fng < MIN_FNG:
        return None, 0, 0, 0, {"skip": f"F&G terlalu rendah ({fng})"}
    if news in ("strong_negative", "negative"):
        return None, 0, 0, 0, {"skip": f"News {news} (skor:{_macro['news_strength']})"}
    if usdt_up:
        return None, 0, 0, 0, {"skip": f"USDT.D naik risk-off ({_macro['usdt_prev']}→{_macro['usdt_d']})"}

    # ── 3. BTC Multi-TF Trend ─────────────────────────────────
    info["btc_15m"] = _macro["btc_trend_15m"]
    info["btc_1h"]  = _macro["btc_trend_1h"]
    info["btc_4h"]  = _macro["btc_trend_4h"]

    # ── 4. Market Breadth ─────────────────────────────────────
    breadth = _macro["market_breadth"]
    info["breadth"] = f"{breadth*100:.0f}%"

    # ── 5. Regime ─────────────────────────────────────────────
    regime = get_regime(symbol)
    info["regime"] = regime

    # ── 6. Candle timing ──────────────────────────────────────
    prev_candle_time = int(df["time"].iloc[-2])
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 60:
        return None, 0, 0, 0, {"skip": "Data tidak cukup"}
    if _last_candle.get(symbol) == prev_candle_time:
        return None, 0, 0, 0, {"skip": "Sudah dianalisa candle ini"}

    # ── 7. TA + Composite Score ───────────────────────────────
    df_closed = run_ta(df_closed)
    ob_imb    = get_ob_imbalance(symbol)
    cum_d     = get_cum_delta(df_closed)
    funding   = get_funding(symbol)

    ta_dir, score, breakdown = calc_composite_score(df_closed, regime, ob_imb, cum_d, funding)
    info["score"]     = f"{score:.1f}/100"
    info["breakdown"] = breakdown

    if ta_dir == "NONE":
        return None, 0, 0, 0, {"skip": f"Score rendah ({score:.1f}/100)"}

    # ── 8. F&G check per direction ────────────────────────────
    if ta_dir == "LONG" and fng > MAX_FNG_LONG:
        return None, 0, 0, 0, {"skip": f"F&G terlalu greedy ({fng}) — euphoria risk LONG"}
    if mcap_bad and ta_dir == "LONG":
        return None, 0, 0, 0, {"skip": f"Global mcap turun {_macro['global_mcap_chg']:.1f}% — skip LONG"}

    # ── 9. BTC Multi-TF check ─────────────────────────────────
    btc_ok, btc_reason = btc_multi_tf_ok_for(ta_dir)
    if not btc_ok:
        return None, 0, 0, 0, {"skip": btc_reason}

    # ── 10. Market Breadth vs direction ───────────────────────
    if ta_dir == "LONG" and breadth < MIN_MARKET_BREADTH:
        return None, 0, 0, 0, {"skip": f"Market breadth rendah ({breadth*100:.0f}%)"}
    if ta_dir == "SHORT" and breadth > 0.65:
        return None, 0, 0, 0, {"skip": f"Market breadth tinggi ({breadth*100:.0f}%), skip SHORT"}

    # ── 11. Regime vs direction ───────────────────────────────
    if ta_dir == "LONG" and regime == "BEAR":
        return None, 0, 0, 0, {"skip": "Regime BEAR — tidak LONG"}
    if ta_dir == "SHORT" and regime == "BULL":
        return None, 0, 0, 0, {"skip": "Regime BULL — tidak SHORT"}

    # ── 12. Volume Spike Confirmation (WAJIB) ─────────────────
    vol_ok, vol_info = check_volume_spike_confirm(df_closed, ta_dir)
    if not vol_ok:
        return None, 0, 0, 0, {"skip": f"Tidak ada volume spike ({vol_info})"}
    info["vol_spike"] = vol_info

    # ── 13. S/R Check (4H swing points) ──────────────────────
    sr_ok, sr_reason = check_sr_clear(symbol, df_closed["close"].iloc[-1], ta_dir)
    if not sr_ok:
        return None, 0, 0, 0, {"skip": f"S/R: {sr_reason}"}
    info["sr"] = "clear"

    # ── 14. Whale filter ──────────────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    info["whale"] = f"{whale_dir}({whale_ratio:.1f}x)"
    if ta_dir == "LONG" and whale_dir == "sell_whale":
        return None, 0, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, 0, {"skip": "Whale buy aktif"}

    # ── 15. Funding extreme ───────────────────────────────────
    info["funding"] = funding
    if ta_dir == "LONG" and funding > 0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu positif (long squeeze risk)"}
    if ta_dir == "SHORT" and funding < -0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu negatif (short squeeze risk)"}

    # ── 16. BB Width (anti choppy) ────────────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    info["bb_width"] = round(bb_width, 4)
    if bb_width < 0.01:
        return None, 0, 0, 0, {"skip": "BB terlalu sempit, choppy"}

    # ── 17. ATR-based SL / TP1 / TP2 ─────────────────────────
    atr   = df_closed["atr"].iloc[-1]
    price = df_closed["close"].iloc[-1]
    if ta_dir == "LONG":
        sl_price  = round(price - ATR_SL_MULT  * atr, 8)
        tp1_price = round(price + ATR_TP1_MULT * atr, 8)
        tp2_price = round(price + ATR_TP2_MULT * atr, 8)
    else:
        sl_price  = round(price + ATR_SL_MULT  * atr, 8)
        tp1_price = round(price - ATR_TP1_MULT * atr, 8)
        tp2_price = round(price - ATR_TP2_MULT * atr, 8)

    sl_pct = abs(price - sl_price) / price
    if sl_pct > 0.04:
        return None, 0, 0, 0, {"skip": f"ATR terlalu besar (SL={sl_pct*100:.1f}%)"}

    _last_candle[symbol] = prev_candle_time
    info["atr_sl_pct"] = f"{sl_pct*100:.2f}%"
    info["ob"]   = ob_imb
    info["ta"]   = ta_dir
    return ta_dir, sl_price, tp1_price, tp2_price, info

# ════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════
def open_trade(symbol, side, sl_price, tp1_price, tp2_price, info):
    """
    Buka posisi full.
    Partial TP akan dieksekusi di manage_positions saat harga kena TP1.
    """
    try:
        set_leverage(symbol)
        price = get_price(symbol)
        qty   = calc_qty(symbol, price, fraction=1.0)
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == "LONG" else SIDE_SELL,
            type=ORDER_TYPE_MARKET, quantity=qty)

        entry    = get_price(symbol)
        trail_sl = entry * (1 - TRAIL_PCT) if side == "LONG" else entry * (1 + TRAIL_PCT)

        open_positions[symbol] = {
            "side":      side,
            "entry":     entry,
            "qty":       qty,
            "qty_remain": qty,     # sisa setelah partial TP
            "sl":        sl_price,
            "tp1":       tp1_price,
            "tp2":       tp2_price,
            "peak":      entry,
            "trail_sl":  trail_sl,
            "trailing_active": False,
            "tp1_hit":   False,    # flag: sudah kena TP1?
            "be_active": False,    # flag: break-even stop sudah aktif?
        }
        sl_pct  = abs(entry - sl_price) / entry * 100
        tp1_pct = abs(tp1_price - entry) / entry * 100
        tp2_pct = abs(tp2_price - entry) / entry * 100
        score   = info.get("score", "?")
        print(f"  ✅ [{symbol}] {side} @{entry:.5f} qty={qty}")
        print(f"     SL:{sl_price:.5f}(-{sl_pct:.2f}%) TP1:{tp1_price:.5f}(+{tp1_pct:.2f}%) TP2:{tp2_price:.5f}(+{tp2_pct:.2f}%)")
        print(f"     Score:{score} BTC:{info.get('btc_15m','?')}/{info.get('btc_1h','?')}/{info.get('btc_4h','?')}")
        print(f"     Breadth:{info.get('breadth','?')} Regime:{info.get('regime','?')} Vol:{info.get('vol_spike','?')}")
        print(f"     Whale:{info.get('whale','?')} SR:{info.get('sr','?')} Funding:{info.get('funding',0):.4f}")
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal entry: {e}")

def partial_close(symbol, reason="TP1"):
    """Tutup 50% posisi, aktifkan break-even stop pada sisanya."""
    global _consec_loss
    pos = open_positions.get(symbol)
    if pos is None: return

    try:
        amt = get_exchange_amt(symbol)
        if amt is None or amt == 0:
            pos["tp1_hit"] = True
            return

        # Tutup 50%
        close_qty = round_step(abs(amt) * 0.5, get_sym_info(symbol)["step"])
        close_qty = max(close_qty, get_sym_info(symbol)["minQty"])

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=close_qty,
            reduceOnly=True)

        exit_price = get_price(symbol)
        side  = pos["side"]
        pnl   = (exit_price - pos["entry"]) * close_qty if side == "LONG" \
                else (pos["entry"] - exit_price) * close_qty
        pct   = pnl / (pos["entry"] * close_qty) * 100

        print(f"  🎯 [{symbol}] PARTIAL TP1 — {reason}")
        print(f"     💛 P&L (50%): {pnl:+.4f} USDT ({pct:+.2f}%)")

        # Update posisi: sisa 50% lanjut dengan break-even SL
        pos["tp1_hit"]   = True
        pos["qty_remain"] = abs(amt) - close_qty
        pos["be_active"] = True
        # Pindah SL ke entry price (break-even)
        pos["sl"]          = pos["entry"]
        # Aktifkan trailing untuk sisa posisi
        pos["trailing_active"] = True
        pos["peak"]            = exit_price
        pos["trail_sl"]        = exit_price * (1 - TRAIL_PCT) if side == "LONG" \
                                 else exit_price * (1 + TRAIL_PCT)

        print(f"     🔒 Break-even SL aktif @{pos['entry']:.5f} | Trailing aktif")
        trade_log.append({"symbol": symbol, "side": side,
                          "pnl": round(pnl, 4), "reason": f"Partial {reason}"})
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal partial close: {e}")
        pos["tp1_hit"] = True  # jangan loop terus

def close_trade(symbol, reason=""):
    global _consec_loss, _in_cooldown
    try:
        amt = get_exchange_amt(symbol)
        if amt is None:
            print(f"  ⚠️  [{symbol}] Query gagal, tunda close")
            return False
        if amt == 0:
            if symbol in open_positions:
                pos   = open_positions[symbol]
                exit_ = get_price(symbol)
                if exit_ > 0:
                    pnl = (exit_ - pos["entry"]) * pos["qty_remain"] if pos["side"] == "LONG" \
                          else (pos["entry"] - exit_) * pos["qty_remain"]
                    pct = pnl / (pos["entry"] * pos["qty_remain"]) * 100 if pos["qty_remain"] > 0 else 0
                    print(f"  ⚠️  [{symbol}] Sudah tutup di exchange — Est P&L: {pnl:+.4f}U ({pct:+.2f}%)")
                    trade_log.append({"symbol": symbol, "side": pos["side"],
                                      "pnl": round(pnl, 4), "reason": "External close"})
                    _update_loss_streak(pnl)
                open_positions.pop(symbol, None)
            return True

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=abs(amt), reduceOnly=True)

        if symbol in open_positions:
            pos   = open_positions[symbol]
            exit_ = get_price(symbol)
            # Hitung P&L sisa posisi saja
            qty_r = pos.get("qty_remain", pos["qty"])
            pnl   = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                    else (pos["entry"] - exit_) * qty_r
            pct   = pnl / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
            emoji = "🟢" if pnl >= 0 else "🔴"
            be_tag = " [BE]" if pos.get("be_active") else ""
            print(f"  💰 [{symbol}] CLOSED — {reason}{be_tag}")
            print(f"     {emoji} P&L (sisa 50%): {pnl:+.4f} USDT ({pct:+.2f}%)")
            trade_log.append({"symbol": symbol, "side": pos["side"],
                              "pnl": round(pnl, 4), "reason": reason})
            _update_loss_streak(pnl)

        open_positions.pop(symbol, None)
        return True
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal close: {e}")
        return False

def _update_loss_streak(pnl):
    global _consec_loss, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        if _consec_loss >= MAX_CONSEC_LOSS:
            btc_bad     = _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < COOLDOWN_BREADTH_MAX
            if btc_bad or breadth_bad:
                _in_cooldown = True
                reasons = []
                if btc_bad:     reasons.append(f"BTC {_macro['btc_trend_15m']}")
                if breadth_bad: reasons.append(f"breadth {_macro['market_breadth']*100:.0f}%")
                print(f"  🧊 {MAX_CONSEC_LOSS} loss + market buruk ({', '.join(reasons)}) → Cooldown!")
            else:
                print(f"  ⚡ {MAX_CONSEC_LOSS} loss tapi market masih oke → lanjut trading")
                _consec_loss = 0
    else:
        _consec_loss = 0
        if _in_cooldown:
            _in_cooldown = False
            print(f"  ✅ Win! Cooldown diakhiri.")

# ════════════════════════════════════════════════════
#  POSITION MANAGEMENT (dengan Partial TP + BE Stop)
# ════════════════════════════════════════════════════
def manage_positions():
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_price(symbol)
        if price == 0:
            print(f"  ⚠️  [{symbol}] Tidak bisa get price, skip")
            continue

        entry = pos["entry"]
        side  = pos["side"]

        # ── Emergency exits ───────────────────────────────────
        if _macro["news"] in ("strong_negative",):
            close_trade(symbol, "🚨 Emergency — strong bad news")
            continue

        if side == "LONG" and _macro["btc_trend_1h"] == "BEAR" and _macro["btc_trend_4h"] == "BEAR":
            # Hanya exit kalau KEDUA 1H dan 4H sudah bear (bukan hanya 15m noise)
            close_trade(symbol, "⚡ BTC 1H+4H BEAR — emergency exit LONG")
            continue

        if side == "SHORT" and _macro["btc_trend_1h"] == "BULL" and _macro["btc_trend_4h"] == "BULL":
            close_trade(symbol, "⚡ BTC 1H+4H BULL — emergency exit SHORT")
            continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            # ── TP1: tutup 50% ────────────────────────────────
            if not pos["tp1_hit"] and price >= pos["tp1"]:
                partial_close(symbol, "TP1")
                continue  # lanjut ke iterasi berikutnya untuk handle sisa

            # ── Trailing (aktif setelah TP1 atau profit threshold) ──
            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 - TRAIL_PCT)

            # ── TP2: tutup sisa ───────────────────────────────
            if pos["tp1_hit"] and price >= pos["tp2"]:
                close_trade(symbol, "✨ TP2 (sisa 50%)")
                continue

            # ── Trailing Stop ─────────────────────────────────
            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop")
                continue

            # ── SL / Break-even Stop ──────────────────────────
            if price <= pos["sl"]:
                reason = "🔒 Break-even Stop" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason)
                continue

            # ── Status print ──────────────────────────────────
            pnl_now = (price - entry) * pos.get("qty_remain", pos["qty"])
            be_tag  = " [BE]" if pos.get("be_active") else ""
            tp_tag  = f" TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f" TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] LONG @{entry:.4f}→{price:.4f}{be_tag} | {pnl_now:+.3f}U{tsl}{tp_tag}")

        else:  # SHORT
            profit_pct = (entry - price) / entry

            # ── TP1: tutup 50% ────────────────────────────────
            if not pos["tp1_hit"] and price <= pos["tp1"]:
                partial_close(symbol, "TP1")
                continue

            # ── Trailing ──────────────────────────────────────
            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 + TRAIL_PCT)

            # ── TP2: tutup sisa ───────────────────────────────
            if pos["tp1_hit"] and price <= pos["tp2"]:
                close_trade(symbol, "✨ TP2 (sisa 50%)")
                continue

            # ── Trailing Stop ─────────────────────────────────
            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop")
                continue

            # ── SL / Break-even Stop ──────────────────────────
            if price >= pos["sl"]:
                reason = "🔒 Break-even Stop" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason)
                continue

            # ── Status print ──────────────────────────────────
            pnl_now = (entry - price) * pos.get("qty_remain", pos["qty"])
            be_tag  = " [BE]" if pos.get("be_active") else ""
            tp_tag  = f" TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f" TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] SHORT @{entry:.4f}→{price:.4f}{be_tag} | {pnl_now:+.3f}U{tsl}{tp_tag}")

# ════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════
def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"] > 0)
    n     = len(trade_log)
    wr    = wins / n * 100 if n else 0
    cd    = f" | 🧊 Cooldown ({cooldown_reason()})" if _in_cooldown else ""
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% W:{wins} L:{n-wins} | P&L:{total:+.4f}U | streak:{_consec_loss}L{cd}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:40]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    print("🤖 Bot v9 — Enhanced Entry + Partial TP + Multi-TF BTC")
    print(f"   Leverage      : {LEVERAGE}x | Order: ${ORDER_USDT} USDT")
    print(f"   SL/TP         : {ATR_SL_MULT}x ATR SL | TP1:{ATR_TP1_MULT}x (50%) | TP2:{ATR_TP2_MULT}x (50%)")
    print(f"   Break-even    : aktif setelah TP1 tercapai, SL pindah ke entry")
    print(f"   Trailing      : aktif setelah +{TRAIL_TRIGGER*100}% (juga pada sisa 50% setelah TP1)")
    print(f"   Min Score     : {MIN_COMPOSITE_SCORE}/100 (composite weighted)")
    print(f"   Min F&G       : {MIN_FNG} (long skip kalau >{MAX_FNG_LONG})")
    print(f"   BTC Filter    : 15m + 1H + 4H harus searah")
    print(f"   Volume Spike  : wajib ada {MIN_VOLUME_SPIKE}x dalam 3 candle terakhir")
    print(f"   S/R Check     : 4H swing high/low, buffer {SR_BUFFER*100:.1f}%")
    print(f"   Market Breadth: min {MIN_MARKET_BREADTH*100:.0f}% bullish untuk LONG")
    print(f"   Smart Cooldown: {MAX_CONSEC_LOSS} loss + market buruk → pause")
    print(f"   Max Posisi    : {MAX_POSITIONS}\n")

    print("  ⏳ Setup...")
    symbols = validate_symbols()
    for s in symbols: get_sym_info(s)
    refresh_macro()
    print(f"  ✅ {len(symbols)} symbols | F&G:{_macro['fng']} | "
          f"BTC 15m:{_macro['btc_trend_15m']} 1H:{_macro['btc_trend_1h']} 4H:{_macro['btc_trend_4h']} | "
          f"News:{_macro['news']}\n")

    cycle = 0
    while True:
        cycle += 1
        refresh_macro()

        if _in_cooldown:
            is_cooldown_active()  # trigger cek recover

        manage_positions()

        cd_info = f" 🧊 COOLDOWN ({cooldown_reason()})" if _in_cooldown else ""
        print(f"\n{'='*72}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | "
              f"USDT:{_macro['usdt_d']}% | News:{_macro['news']}(skor:{_macro['news_strength']}){cd_info}")
        print(f"  📈 BTC 15m:{_macro['btc_trend_15m']} | 1H:{_macro['btc_trend_1h']} | 4H:{_macro['btc_trend_4h']}")
        print(f"  🌍 Breadth:{_macro['market_breadth']*100:.0f}% bullish | MCap24h:{_macro['global_mcap_chg']:+.1f}%")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*72}")

        skipped    = 0
        candidates = []

        if len(open_positions) < MAX_POSITIONS and _macro["news"] not in ("strong_negative", "negative") \
           and not _in_cooldown:
            for symbol in symbols:
                if symbol in open_positions: continue
                df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 220)
                if df is None or len(df) < 70: continue
                side, sl, tp1, tp2, info = should_enter(symbol, df)
                if side:
                    candidates.append((symbol, side, sl, tp1, tp2, info))
                else:
                    skipped += 1

            if candidates:
                # Sort by composite score (descending)
                candidates.sort(
                    key=lambda x: float(x[5].get("score", "0").split("/")[0]),
                    reverse=True
                )
                print(f"\n  🎯 {len(candidates)} setup valid | {skipped} di-skip")
                for sym, side, sl, tp1, tp2, info in candidates[:3]:
                    print(f"     ⭐ {sym} {side} | Score:{info.get('score','?')} | "
                          f"Vol:{info.get('vol_spike','?')} | SR:{info.get('sr','?')} | "
                          f"BTC:{info.get('btc_15m','?')}/{info.get('btc_1h','?')}/{info.get('btc_4h','?')}")
                for sym, side, sl, tp1, tp2, info in candidates:
                    if len(open_positions) >= MAX_POSITIONS: break
                    open_trade(sym, side, sl, tp1, tp2, info)
            else:
                print(f"  ⏳ {skipped} coins di-scan, belum ada setup valid")
        else:
            if _in_cooldown:
                print(f"  🧊 Cooldown — {cooldown_reason()}")
                print(f"     Lanjut kalau BTC → BULL/MILD_BULL DAN breadth > {COOLDOWN_BREADTH_MIN*100:.0f}%")
            else:
                print(f"  ⏸️  Posisi penuh atau kondisi tidak aman")

        print_summary()
        print(f"\n  ⏱️  {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
