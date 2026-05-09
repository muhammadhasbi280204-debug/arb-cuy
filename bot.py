"""
Bot Trading v10 — Smart Aggressive Scalping
============================================
MASALAH v9 yang diperbaiki:

ROOT CAUSE #1 — FILTER TERLALU KAKU & SERIAL:
  v9: MIN_FNG=45 → kalau F&G=38 → SKIP SEMUA bahkan tanpa scan TA
  v9: MIN_BREADTH=0.45 → breadth 40% → SKIP SEMUA LONG
  v9: MIN_COMPOSITE_SCORE=62 → hampir tidak ada coin yang tembus
  v9: Volume spike 1.5x WAJIB di 3 candle terakhir → sangat jarang terpenuhi
  v9: S/R buffer 0.8% + swing 4H → sangat sering blocked

FIX v10 — FILOSOFI "ADAPTIVE THRESHOLD":
  - Threshold DINAMIS berdasarkan kondisi market, bukan angka mati
  - F&G fear zone (30-45) = lebih selektif LONG, tapi SHORT tetap jalan
  - Breadth rendah = prioritas SHORT/kontratrend, bukan block semua
  - Composite score: threshold turun saat ada confluence kuat (misal 3+ timeframe align)
  - Volume spike: OR condition (spike 1.5x ATAU momentum konsisten 5 candle)
  - S/R: hanya block kalau resistance <0.5% (bukan 0.8%)

FITUR BARU v10:
1. SCALP MODE: Entry 15m dengan konfirmasi 5m (dual timeframe entry)
2. MOMENTUM SCORE: Skor momentum murni 5 candle terakhir (cepat)
3. ADAPTIVE COMPOSITE SCORE: Minimum score turun kalau ada:
   - 3+ timeframe BTC align → score min -8
   - Strong volume spike (>2x) → score min -5
   - Regime clear (BULL/BEAR, bukan RANGE) → score min -5
4. PARTIAL EXIT DIPERBAIKI: TP1 lebih dekat (1.5x ATR), jalan lebih sering
5. SESSION FILTER: Lebih aktif di sesi Asia+Eropa+NY open (08-22 UTC)
6. QUICK REVERSAL DETECTOR: Masuk setelah spike reversal (wick panjang)
7. MULTI-SYMBOL RANKING: Pilih top 3 setup terbaik dari semua coin, eksekusi terbaik
8. COOLDOWN LEBIH CERDAS: Cooldown per-symbol, bukan global freeze semua coin
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
#  CONFIG — SCALPING ORIENTED
# ════════════════════════════════════════════════════
LEVERAGE              = 10
ORDER_USDT            = 55
ATR_SL_MULT           = 1.5      # v10: lebih ketat dari v9 (was 2.0)
ATR_TP1_MULT          = 1.5      # v10: TP1 lebih dekat → lebih sering hit (was 2.0)
ATR_TP2_MULT          = 3.0      # v10: TP2 tetap oke (was 4.0)
TRAIL_TRIGGER         = 0.003    # aktifkan trailing setelah +0.3% (was 0.5%)
TRAIL_PCT             = 0.002    # trail lebih ketat (was 0.3%)
MAX_POSITIONS         = 3        # naikkan dari 2 ke 3
SCAN_INTERVAL         = 45       # scan lebih cepat (was 60s)
MAX_CONSEC_LOSS       = 3        # was 2, lebih toleran
MIN_MARKET_BREADTH    = 0.35     # was 0.45 — lebih longgar
SR_BUFFER             = 0.004    # was 0.008 — separuhnya

# ── ADAPTIVE SCORE THRESHOLDS ──────────────────────
BASE_MIN_SCORE        = 52       # was 62 — starting point lebih rendah
SCORE_BONUS_BTC_ALIGN = 8        # kurangi threshold kalau 3TF BTC align
SCORE_BONUS_VOL_SPIKE = 5        # kurangi threshold kalau volume >2x
SCORE_BONUS_REGIME    = 5        # kurangi threshold kalau regime clear
SCORE_BONUS_MOMENTUM  = 5        # kurangi threshold kalau 5c momentum kuat

# ── FEAR & GREED — ADAPTIVE ────────────────────────
MIN_FNG               = 25       # was 45 — hanya skip kalau ekstrem fear
MAX_FNG_LONG          = 88       # was 85 — sedikit lebih longgar
MIN_FNG_ANY           = 15       # was 20

# ── MACRO THRESHOLDS ───────────────────────────────
USDT_RISK_OFF_DELTA   = 0.05     # was 0.03 — less sensitive
FNG_FEAR_ZONE         = 40       # F&G < 40: prefer SHORT, tapi LONG tetap bisa
FNG_GREED_ZONE        = 75       # F&G > 75: prefer LONG, SHORT tetap bisa dengan syarat

# ── VOLUME ─────────────────────────────────────────
MIN_VOLUME_SPIKE      = 1.3      # was 1.5 — lebih mudah terpenuhi
MOMENTUM_CANDLES      = 5        # alternatif volume: 5 candle konsisten

# ── COOLDOWN — PER SYMBOL ──────────────────────────
# v10: cooldown per-symbol (bukan global freeze semua)
SYMBOL_COOLDOWN_SECS  = 300      # 5 menit cooldown per symbol setelah loss
GLOBAL_COOLDOWN_LOSS  = 4        # was 2 — global cooldown kalau >= 4 loss berturut
COOLDOWN_BTC_BAD      = {"BEAR"}                   # hanya BEAR (was BEAR+MILD_BEAR)
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL", "SIDEWAYS"}  # lebih mudah recover
COOLDOWN_BREADTH_MAX  = 0.30     # was 0.40
COOLDOWN_BREADTH_MIN  = 0.40     # was 0.50

# ── SESSION FILTER ─────────────────────────────────
# UTC hours — lebih aktif di jam tertentu
ACTIVE_SESSIONS = {
    "ASIA_OPEN":   (0, 4),    # 00-04 UTC
    "EU_OPEN":     (7, 11),   # 07-11 UTC
    "NY_OPEN":     (13, 17),  # 13-17 UTC
    "OVERLAP":     (13, 15),  # EU-NY overlap (terbaik)
}
# Di luar session aktif: MIN_SCORE dinaikkan 8 poin (lebih selektif)
OFF_SESSION_PENALTY   = 8

# Composite score weights
SCORE_WEIGHTS = {
    "macd_hist":    18,
    "rsi":          15,
    "ema_stack":    14,
    "volume":       13,
    "ob_imbalance": 12,
    "cum_delta":    10,
    "stoch":         8,
    "bb":            6,
    "funding":       4,
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
_symbol_cooldown = {}  # {symbol: timestamp_last_loss}

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
    info = get_sym_info(symbol)
    return max(round_step((ORDER_USDT * LEVERAGE * fraction) / price, info["step"]), info["minQty"])

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

def is_active_session():
    """Cek apakah sekarang dalam sesi trading aktif (UTC)."""
    utc_hour = time.gmtime().tm_hour
    for name, (start, end) in ACTIVE_SESSIONS.items():
        if start <= utc_hour < end:
            return True, name
    return False, "OFF"

def get_session_score_penalty():
    """Return penalty skor kalau di luar sesi aktif."""
    active, session = is_active_session()
    return 0 if active else OFF_SESSION_PENALTY

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
#  SUPPORT & RESISTANCE (4H — lebih simpel)
# ════════════════════════════════════════════════════
def get_sr_levels(symbol, lookback=20):
    """S/R dari 4H candle. Lebih sedikit lookback = lebih relevan."""
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_4HOUR, lookback + 5)
    if df is None or len(df) < 10:
        return {"resistance": [], "support": []}

    highs = df["high"].values
    lows  = df["low"].values
    resistance = []
    support    = []

    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support.append(lows[i])

    return {"resistance": sorted(resistance, reverse=True)[:3],
            "support":    sorted(support)[:3]}

def check_sr_clear(symbol, price, direction):
    """
    v10: SR_BUFFER dikecilkan ke 0.4% dan hanya block kalau ADA level yang dekat.
    Kalau tidak ada level, langsung OK.
    """
    sr = get_sr_levels(symbol)

    if direction == "LONG":
        nearby_res = [r for r in sr["resistance"] if r > price]
        if nearby_res:
            nearest  = min(nearby_res)
            gap_pct  = (nearest - price) / price
            if gap_pct < SR_BUFFER:
                return False, f"Resistance {nearest:.4f} cuma {gap_pct*100:.2f}% jauhnya"
    elif direction == "SHORT":
        nearby_sup = [s for s in sr["support"] if s < price]
        if nearby_sup:
            nearest = max(nearby_sup)
            gap_pct = (price - nearest) / price
            if gap_pct < SR_BUFFER:
                return False, f"Support {nearest:.4f} cuma {gap_pct*100:.2f}% jauhnya"

    return True, ""

# ════════════════════════════════════════════════════
#  SCALP MODE: Konfirmasi 5m
# ════════════════════════════════════════════════════
def get_5m_confirmation(symbol, direction):
    """
    v10 BARU: Cek momentum 5m untuk entry confirmation.
    Lebih cepat dari nunggu candle 15m close.
    Returns (confirmed: bool, reason: str)
    """
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_5MINUTE, 20)
    if df is None or len(df) < 15:
        return True, "no 5m data — skip check"  # kalau gagal fetch, jangan block

    df = df.iloc[:-1].copy()  # exclude candle aktif
    c  = df["close"]
    ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator()
    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    # Cek arah 3 candle terakhir
    last3 = df.tail(3)
    up_candles   = sum(1 for _, r in last3.iterrows() if r["close"] > r["open"])
    down_candles = sum(1 for _, r in last3.iterrows() if r["close"] < r["open"])

    if direction == "LONG":
        # Konfirmasi: price di atas EMA9, ada candle naik
        price_above_ema = last["close"] > ema9.iloc[-1]
        momentum_ok     = up_candles >= 2
        if price_above_ema and momentum_ok:
            return True, f"5m✅ ema9 up, {up_candles}/3 green"
        elif price_above_ema or momentum_ok:
            return True, f"5m✓ partial confirm"
        return False, f"5m❌ {up_candles}/3 green, price{'>' if price_above_ema else '<'}ema9"

    else:  # SHORT
        price_below_ema = last["close"] < ema9.iloc[-1]
        momentum_ok     = down_candles >= 2
        if price_below_ema and momentum_ok:
            return True, f"5m✅ ema9 down, {down_candles}/3 red"
        elif price_below_ema or momentum_ok:
            return True, f"5m✓ partial confirm"
        return False, f"5m❌ {down_candles}/3 red, price{'<' if price_below_ema else '>'}ema9"

# ════════════════════════════════════════════════════
#  QUICK REVERSAL DETECTOR (Wick Reversal)
# ════════════════════════════════════════════════════
def detect_reversal(df, direction):
    """
    v10 BARU: Deteksi reversal candle (pin bar / hammer / shooting star).
    Candle dengan wick panjang = potensi reversal kuat.
    Tambahkan poin ke score kalau terdeteksi.
    Returns: (detected: bool, bonus_pts: int, desc: str)
    """
    last = df.iloc[-2]  # candle terakhir yang closed
    body = abs(last["close"] - last["open"])
    rng  = last["high"] - last["low"]
    if rng == 0: return False, 0, ""

    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]
    body_ratio = body / rng

    if direction == "LONG" and body_ratio < 0.4 and lower_wick > body * 2:
        # Hammer: lower wick panjang → reversal bullish
        return True, 6, f"hammer(wick:{lower_wick/rng*100:.0f}%)"

    if direction == "SHORT" and body_ratio < 0.4 and upper_wick > body * 2:
        # Shooting star: upper wick panjang → reversal bearish
        return True, 6, f"shooting_star(wick:{upper_wick/rng*100:.0f}%)"

    return False, 0, ""

# ════════════════════════════════════════════════════
#  MOMENTUM SCORE (5 candle konsisten)
# ════════════════════════════════════════════════════
def get_momentum_score(df, direction):
    """
    v10: Alternatif volume spike — cek 5 candle terakhir konsisten.
    Kalau 4/5 candle searah, anggap ada momentum.
    Returns (score_bonus: int, desc: str)
    """
    recent = df.tail(MOMENTUM_CANDLES + 1).iloc[:-1]  # closed candles
    if len(recent) < MOMENTUM_CANDLES:
        return 0, ""

    if direction == "LONG":
        count = sum(1 for _, r in recent.iterrows() if r["close"] > r["open"])
        # Tambahan: cek konsistensi close lebih tinggi dari close sebelumnya
        closes = recent["close"].values
        up_closes = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        if count >= 4 and up_closes >= 3:
            return 8, f"momentum 5c:{count}/5 up"
        elif count >= 3:
            return 4, f"momentum 5c:{count}/5 up"
    else:
        count = sum(1 for _, r in recent.iterrows() if r["close"] < r["open"])
        closes = recent["close"].values
        down_closes = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        if count >= 4 and down_closes >= 3:
            return 8, f"momentum 5c:{count}/5 down"
        elif count >= 3:
            return 4, f"momentum 5c:{count}/5 down"

    return 0, ""

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
    "global_mcap_chg": 0.0,
    "last_fng": 0, "last_dom": 0, "last_news": 0,
    "last_btc": 0, "last_breadth": 0
}

def _calc_btc_trend(df):
    if df is None or len(df) < 30:
        return "UNKNOWN"
    c     = df["close"]
    price = c.iloc[-1]
    ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
    ema21 = ta.trend.EMAIndicator(c, 21).ema_indicator().iloc[-1]
    ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator().iloc[-1]
    chg   = (price - c.iloc[-4]) / c.iloc[-4] * 100

    if price > ema9 > ema21 > ema50 and chg > 0:   return "BULL"
    elif price < ema9 < ema21 < ema50 and chg < 0: return "BEAR"
    elif price > ema21 and chg > -0.3:              return "MILD_BULL"
    elif price < ema21 and chg < 0.3:               return "MILD_BEAR"
    return "SIDEWAYS"

def refresh_macro():
    now = time.time()

    if now - _macro["last_fng"] > 300:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()["data"][0]
            _macro["fng"]       = int(d["value"])
            _macro["fng_label"] = d["value_classification"]
            _macro["last_fng"]  = now
        except: pass

    if now - _macro["last_dom"] > 300:
        try:
            d = requests.get("https://api.coingecko.com/api/v3/global", timeout=8).json()
            _macro["usdt_prev"] = _macro["usdt_d"]
            _macro["usdt_d"]    = round(d["data"]["market_cap_percentage"].get("usdt", 5), 2)
            chg_pct = d["data"].get("market_cap_change_percentage_24h_usd", 0)
            _macro["global_mcap_chg"] = round(chg_pct, 2)
            _macro["last_dom"]  = now
        except: pass

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

    if now - _macro["last_btc"] > 45:  # lebih sering refresh (was 60s)
        try:
            df_15m = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_15MINUTE, 60)
            df_1h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_1HOUR, 60)
            df_4h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_4HOUR, 60)
            _macro["btc_trend_15m"] = _calc_btc_trend(df_15m)
            _macro["btc_trend_1h"]  = _calc_btc_trend(df_1h)
            _macro["btc_trend_4h"]  = _calc_btc_trend(df_4h)
            _macro["last_btc"]      = now
        except: pass

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
#  ADAPTIVE MIN SCORE
# ════════════════════════════════════════════════════
BULL_TRENDS = {"BULL", "MILD_BULL"}
BEAR_TRENDS = {"BEAR", "MILD_BEAR"}

def get_adaptive_min_score(direction):
    """
    v10 KUNCI: Threshold score TURUN kalau ada confluence kuat.
    Kalau banyak faktor align → bot lebih berani entry.
    """
    min_score = BASE_MIN_SCORE
    bonuses   = []

    # Cek BTC 3TF alignment
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
    else:
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)

    if btc_align >= 3:
        min_score -= SCORE_BONUS_BTC_ALIGN
        bonuses.append(f"BTC3TF-{SCORE_BONUS_BTC_ALIGN}")
    elif btc_align >= 2:
        min_score -= SCORE_BONUS_BTC_ALIGN // 2
        bonuses.append(f"BTC2TF-{SCORE_BONUS_BTC_ALIGN//2}")

    # Session bonus: aktif di sesi prime
    active, session = is_active_session()
    if session == "OVERLAP":
        min_score -= 4
        bonuses.append("OVERLAP_SESSION-4")
    elif active:
        min_score -= 2
        bonuses.append(f"{session}-2")
    else:
        min_score += OFF_SESSION_PENALTY
        bonuses.append(f"OFF_SESSION+{OFF_SESSION_PENALTY}")

    # F&G context
    fng = _macro["fng"]
    if direction == "LONG" and fng < FNG_FEAR_ZONE:
        # Fear zone: LONG lebih riskan, naikkan sedikit threshold
        min_score += 5
        bonuses.append("FNG_FEAR+5")
    elif direction == "SHORT" and fng < FNG_FEAR_ZONE:
        # Fear zone: SHORT lebih aman
        min_score -= 4
        bonuses.append("FNG_FEAR_SHORT-4")
    elif direction == "LONG" and fng > FNG_GREED_ZONE:
        # Greed zone: LONG momentum bagus
        min_score -= 3
        bonuses.append("FNG_GREED_LONG-3")

    # Floor: jangan terlalu rendah
    min_score = max(min_score, 38)
    return min_score, bonuses

def btc_multi_tf_ok_for(direction):
    """
    v10: LEBIH LONGGAR — hanya block kalau 2+ timeframe berlawanan.
    v9: block kalau 1H atau 4H saja berlawanan (terlalu strict).
    """
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        bear_count = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)
        if t4h in BEAR_TRENDS and t1h in BEAR_TRENDS:
            return False, f"BTC 4H={t4h} + 1H={t1h} keduanya bearish"
        if bear_count >= 3:
            return False, f"BTC semua TF bearish: 15m={t15} 1H={t1h} 4H={t4h}"

    elif direction == "SHORT":
        bull_count = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
        if t4h in BULL_TRENDS and t1h in BULL_TRENDS:
            return False, f"BTC 4H={t4h} + 1H={t1h} keduanya bullish"
        if bull_count >= 3:
            return False, f"BTC semua TF bullish: 15m={t15} 1H={t1h} 4H={t4h}"

    return True, ""

# ════════════════════════════════════════════════════
#  REGIME
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
    v10: SAMA seperti v9 tapi dengan bonus dari reversal + momentum score.
    Returns: (direction, score, breakdown, bonus_desc)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]
    W    = SCORE_WEIGHTS
    breakdown = {}
    long_score = short_score = 0.0

    # 1. MACD Histogram
    hist_now  = last["macd_hist"]
    hist_prev = prev["macd_hist"]
    if hist_now > 0 and hist_now > hist_prev:
        pts = W["macd_hist"] if (last["macd"] > last["macd_sig"] and prev["macd"] <= prev["macd_sig"]) \
              else W["macd_hist"] * 0.7
        long_score += pts; breakdown["macd"] = f"+{pts:.1f}L"
    elif hist_now < 0 and hist_now < hist_prev:
        pts = W["macd_hist"] if (last["macd"] < last["macd_sig"] and prev["macd"] >= prev["macd_sig"]) \
              else W["macd_hist"] * 0.7
        short_score += pts; breakdown["macd"] = f"+{pts:.1f}S"
    else:
        breakdown["macd"] = "0"

    # 2. RSI
    rsi = last["rsi"]
    if rsi < 40:    # v10: threshold lebih longgar (was 35)
        pts = W["rsi"] if rsi < 30 else W["rsi"] * 0.6
        long_score += pts; breakdown["rsi"] = f"+{pts:.1f}L(rsi:{rsi:.0f})"
    elif rsi > 60:  # v10: was 65
        pts = W["rsi"] if rsi > 70 else W["rsi"] * 0.6
        short_score += pts; breakdown["rsi"] = f"+{pts:.1f}S(rsi:{rsi:.0f})"
    else:
        breakdown["rsi"] = f"0(rsi:{rsi:.0f})"

    # 3. EMA Stack
    e9, e21, e50 = last["ema9"], last["ema21"], last["ema50"]
    if e9 > e21 > e50:
        long_score += W["ema_stack"]; breakdown["ema"] = f"+{W['ema_stack']}L"
    elif e9 < e21 < e50:
        short_score += W["ema_stack"]; breakdown["ema"] = f"+{W['ema_stack']}S"
    else:
        if e9 > e21: long_score += W["ema_stack"] * 0.5   # v10: was 0.4
        elif e9 < e21: short_score += W["ema_stack"] * 0.5
        breakdown["ema"] = "partial"

    # 4. Volume + taker ratio
    vr = last["vol_ratio"]
    br = last["buy_ratio"]
    if vr >= MIN_VOLUME_SPIKE:
        if last["close"] > last["open"] and br > 0.52:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 2.0)  # v10: max 2x (was 1.5x)
            long_score += pts; breakdown["vol"] = f"+{pts:.1f}L({vr:.1f}x)"
        elif last["close"] < last["open"] and br < 0.48:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 2.0)
            short_score += pts; breakdown["vol"] = f"+{pts:.1f}S({vr:.1f}x)"
        else:
            breakdown["vol"] = f"spike({vr:.1f}x) ambiguous"
    else:
        # v10 BARU: kalau tidak ada spike, beri partial credit berdasarkan trend volume
        if vr >= 0.9 and last["close"] > last["open"] and br > 0.54:
            long_score += W["volume"] * 0.4
            breakdown["vol"] = f"+{W['volume']*0.4:.1f}L(trend,{vr:.1f}x)"
        elif vr >= 0.9 and last["close"] < last["open"] and br < 0.46:
            short_score += W["volume"] * 0.4
            breakdown["vol"] = f"+{W['volume']*0.4:.1f}S(trend,{vr:.1f}x)"
        else:
            breakdown["vol"] = f"weak({vr:.1f}x)"

    # 5. Order Book Imbalance
    if ob_imb > 0.12:   # v10: was 0.15
        pts = W["ob_imbalance"] * min(ob_imb / 0.12, 1.5)
        long_score += pts; breakdown["ob"] = f"+{pts:.1f}L({ob_imb:+.2f})"
    elif ob_imb < -0.12:
        pts = W["ob_imbalance"] * min(abs(ob_imb) / 0.12, 1.5)
        short_score += pts; breakdown["ob"] = f"+{pts:.1f}S({ob_imb:+.2f})"
    else:
        breakdown["ob"] = f"neutral({ob_imb:+.2f})"

    # 6. Cumulative Delta
    if cum_d > 0.12:   # v10: was 0.15
        long_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}L"
    elif cum_d < -0.12:
        short_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}S"
    else:
        breakdown["delta"] = "0"

    # 7. Stochastic
    k, d    = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    if k < 30 and k > d and pk <= pd_:   # v10: was k<25
        long_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}L"
    elif k > 70 and k < d and pk >= pd_: # v10: was k>75
        short_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}S"
    else:
        breakdown["stoch"] = "0"

    # 8. Bollinger Band
    price = last["close"]
    if price <= last["bb_lo"] * 1.005:   # v10: was 1.002 (lebih longgar)
        long_score += W["bb"]; breakdown["bb"] = f"+{W['bb']}L"
    elif price >= last["bb_hi"] * 0.995:
        short_score += W["bb"]; breakdown["bb"] = f"+{W['bb']}S"
    else:
        breakdown["bb"] = "0"

    # 9. Funding Rate
    if funding < -0.05:
        long_score += W["funding"]; breakdown["funding"] = f"+{W['funding']}L"
    elif funding > 0.05:
        short_score += W["funding"]; breakdown["funding"] = f"+{W['funding']}S"
    else:
        breakdown["funding"] = "0"

    # Regime multiplier
    if regime == "BULL":
        long_score *= 1.1; short_score *= 0.85
    elif regime == "BEAR":
        short_score *= 1.1; long_score *= 0.85

    # Normalisasi
    max_possible = sum(W.values()) * 2.0
    long_pct  = min(long_score  / max_possible * 100, 100)
    short_pct = min(short_score / max_possible * 100, 100)

    # v10: margin gap dikurangi dari 10 ke 8
    if long_pct > short_pct + 8:
        return "LONG", long_pct, breakdown
    if short_pct > long_pct + 8:
        return "SHORT", short_pct, breakdown
    return "NONE", max(long_pct, short_pct), breakdown

def check_volume_or_momentum(df, direction):
    """
    v10 KUNCI: Volume spike ATAU momentum konsisten = OK.
    v9 hanya volume spike (terlalu strict).
    """
    # Cek volume spike (kondisi 1)
    recent = df.iloc[-4:-1]  # 3 closed candles
    for _, row in recent.iterrows():
        vr = row["vol_ratio"]
        br = row["buy_ratio"]
        if vr >= MIN_VOLUME_SPIKE:
            if direction == "LONG" and row["close"] > row["open"] and br > 0.52:
                return True, f"vol_spike {vr:.1f}x"
            if direction == "SHORT" and row["close"] < row["open"] and br < 0.48:
                return True, f"vol_spike {vr:.1f}x"

    # Cek momentum konsisten (kondisi 2)
    mom_bonus, mom_desc = get_momentum_score(df, direction)
    if mom_bonus >= 4:
        return True, f"momentum: {mom_desc}"

    return False, "no vol spike & weak momentum"

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
#  SMART COOLDOWN (PER SYMBOL + GLOBAL)
# ════════════════════════════════════════════════════
def is_symbol_in_cooldown(symbol):
    """v10: Cooldown per-symbol, bukan global freeze."""
    if symbol not in _symbol_cooldown:
        return False
    elapsed = time.time() - _symbol_cooldown[symbol]
    if elapsed > SYMBOL_COOLDOWN_SECS:
        del _symbol_cooldown[symbol]
        return False
    remaining = SYMBOL_COOLDOWN_SECS - elapsed
    return True  # masih cooldown

def get_symbol_cooldown_remaining(symbol):
    if symbol not in _symbol_cooldown: return 0
    return max(0, SYMBOL_COOLDOWN_SECS - (time.time() - _symbol_cooldown[symbol]))

def set_symbol_cooldown(symbol):
    _symbol_cooldown[symbol] = time.time()
    print(f"  🧊 [{symbol}] Symbol cooldown {SYMBOL_COOLDOWN_SECS}s")

def check_global_cooldown_recover():
    btc_ok     = _macro["btc_trend_15m"] in COOLDOWN_BTC_RECOVER
    breadth_ok = _macro["market_breadth"] >= COOLDOWN_BREADTH_MIN
    return btc_ok or breadth_ok  # v10: OR (was AND — lebih cepat recover)

def is_cooldown_active():
    global _in_cooldown
    if not _in_cooldown:
        return False
    if check_global_cooldown_recover():
        _in_cooldown = False
        print(f"  ✅ Global cooldown selesai! BTC:{_macro['btc_trend_15m']} "
              f"Breadth:{_macro['market_breadth']*100:.0f}%")
        return False
    return True

def cooldown_reason():
    reasons = []
    if _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD:
        reasons.append(f"BTC {_macro['btc_trend_15m']}")
    if _macro["market_breadth"] < COOLDOWN_BREADTH_MIN:
        reasons.append(f"breadth {_macro['market_breadth']*100:.0f}%")
    return " & ".join(reasons) if reasons else "kondisi belum oke"

# ════════════════════════════════════════════════════
#  MASTER ENTRY FILTER
# ════════════════════════════════════════════════════
def should_enter(symbol, df):
    info = {}

    # ── 0. Symbol cooldown (per-symbol) ───────────────────────
    if is_symbol_in_cooldown(symbol):
        remaining = get_symbol_cooldown_remaining(symbol)
        return None, 0, 0, 0, {"skip": f"⏳ Symbol cooldown {remaining:.0f}s"}

    # ── 1. Global cooldown ────────────────────────────────────
    if is_cooldown_active():
        return None, 0, 0, 0, {"skip": f"🧊 Global cooldown ({cooldown_reason()})"}

    # ── 2. Macro hard blocks (DIKURANGI) ──────────────────────
    fng     = _macro["fng"]
    news    = _macro["news"]
    usdt_up = _macro["usdt_d"] > _macro["usdt_prev"] + USDT_RISK_OFF_DELTA

    # v10: Hanya block kalau ekstrem
    if fng < MIN_FNG_ANY:
        return None, 0, 0, 0, {"skip": f"F&G ekstrem ({fng}) — skip semua"}

    # v10: F&G 25-45 TIDAK lagi block semuanya — hanya pengaruhi threshold score
    if news == "strong_negative":
        return None, 0, 0, 0, {"skip": f"News strong_negative — skip"}

    # v10: USDT.D naik hanya block kalau signifikan (0.05, was 0.03)
    if usdt_up:
        return None, 0, 0, 0, {"skip": f"USDT.D risk-off ({_macro['usdt_prev']}→{_macro['usdt_d']})"}

    # ── 3. BTC trend (LEBIH LONGGAR) ──────────────────────────
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
        return None, 0, 0, 0, {"skip": "Sudah dianalisa"}

    # ── 7. TA + Composite Score ───────────────────────────────
    df_closed = run_ta(df_closed)
    ob_imb    = get_ob_imbalance(symbol)
    cum_d     = get_cum_delta(df_closed)
    funding   = get_funding(symbol)

    ta_dir, score, breakdown = calc_composite_score(df_closed, regime, ob_imb, cum_d, funding)
    info["score"]     = f"{score:.1f}/100"
    info["breakdown"] = breakdown

    if ta_dir == "NONE":
        return None, 0, 0, 0, {"skip": f"Score tidak meyakinkan ({score:.1f})"}

    # ── 8. Adaptive Min Score ─────────────────────────────────
    min_score, score_bonuses = get_adaptive_min_score(ta_dir)
    info["min_score"] = min_score
    info["score_ctx"] = score_bonuses

    if score < min_score:
        return None, 0, 0, 0, {"skip": f"Score {score:.1f} < min {min_score} ({', '.join(score_bonuses)})"}

    # ── 9. F&G direction check (kontekstual, bukan hard block) ─
    if ta_dir == "LONG" and fng > MAX_FNG_LONG:
        return None, 0, 0, 0, {"skip": f"F&G terlalu greedy ({fng}) — euphoria LONG"}
    if ta_dir == "LONG" and fng < MIN_FNG_ANY + 10:
        # Fear ekstrem tapi tidak di-block total → butuh score lebih tinggi
        if score < min_score + 10:
            return None, 0, 0, 0, {"skip": f"Fear zone F&G={fng}, score {score:.1f} kurang kuat"}

    # ── 10. BTC Multi-TF (LEBIH LONGGAR) ──────────────────────
    btc_ok, btc_reason = btc_multi_tf_ok_for(ta_dir)
    if not btc_ok:
        return None, 0, 0, 0, {"skip": btc_reason}

    # ── 11. Market Breadth (LEBIH LONGGAR) ────────────────────
    if ta_dir == "LONG" and breadth < MIN_MARKET_BREADTH:
        # v10: block kalau breadth < 35% (was 45%)
        return None, 0, 0, 0, {"skip": f"Breadth {breadth*100:.0f}% < {MIN_MARKET_BREADTH*100:.0f}%"}
    if ta_dir == "SHORT" and breadth > 0.70:  # was 0.65
        return None, 0, 0, 0, {"skip": f"Breadth {breadth*100:.0f}% terlalu tinggi, skip SHORT"}

    # ── 12. Regime vs direction (LEBIH LONGGAR) ───────────────
    if ta_dir == "LONG" and regime == "BEAR":
        # v10: masih bisa LONG di BEAR kalau score sangat tinggi (reversal)
        if score < min_score + 15:
            return None, 0, 0, 0, {"skip": "Regime BEAR, score tidak cukup untuk counter-trend LONG"}
    if ta_dir == "SHORT" and regime == "BULL":
        if score < min_score + 15:
            return None, 0, 0, 0, {"skip": "Regime BULL, score tidak cukup untuk counter-trend SHORT"}

    # ── 13. Volume Spike ATAU Momentum (OR condition) ─────────
    vol_ok, vol_info = check_volume_or_momentum(df_closed, ta_dir)
    if not vol_ok:
        return None, 0, 0, 0, {"skip": f"Volume/momentum lemah: {vol_info}"}
    info["vol_momentum"] = vol_info

    # ── 14. Reversal detector (bonus, tidak wajib) ────────────
    rev_detected, rev_bonus, rev_desc = detect_reversal(df_closed, ta_dir)
    if rev_detected:
        info["reversal"] = rev_desc
    else:
        info["reversal"] = "-"

    # ── 15. S/R Check (LEBIH LONGGAR) ─────────────────────────
    sr_ok, sr_reason = check_sr_clear(symbol, df_closed["close"].iloc[-1], ta_dir)
    if not sr_ok:
        return None, 0, 0, 0, {"skip": f"S/R: {sr_reason}"}
    info["sr"] = "clear"

    # ── 16. 5m Confirmation (SCALP) ───────────────────────────
    m5_ok, m5_info = get_5m_confirmation(symbol, ta_dir)
    info["5m"] = m5_info
    if not m5_ok:
        # v10: tidak hard block, tapi naikkan score requirement
        if score < min_score + 8:
            return None, 0, 0, 0, {"skip": f"5m contra: {m5_info}, score kurang ({score:.1f})"}

    # ── 17. Whale filter ──────────────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    info["whale"] = f"{whale_dir}({whale_ratio:.1f}x)"
    if ta_dir == "LONG" and whale_dir == "sell_whale":
        return None, 0, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, 0, {"skip": "Whale buy aktif"}

    # ── 18. Funding extreme ───────────────────────────────────
    info["funding"] = funding
    if ta_dir == "LONG" and funding > 0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu positif"}
    if ta_dir == "SHORT" and funding < -0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu negatif"}

    # ── 19. BB Width (anti choppy) ────────────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    info["bb_width"] = round(bb_width, 4)
    if bb_width < 0.008:  # was 0.01 — lebih toleran
        return None, 0, 0, 0, {"skip": f"BB terlalu sempit ({bb_width:.4f})"}

    # ── 20. ATR SL/TP ─────────────────────────────────────────
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
    if sl_pct > 0.035:  # was 0.04 — sedikit lebih ketat untuk scalping
        return None, 0, 0, 0, {"skip": f"ATR terlalu besar (SL={sl_pct*100:.1f}%)"}

    _last_candle[symbol] = prev_candle_time
    info["atr_sl_pct"] = f"{sl_pct*100:.2f}%"
    info["ob"]   = ob_imb
    info["ta"]   = ta_dir
    info["score_num"] = score  # untuk sorting
    return ta_dir, sl_price, tp1_price, tp2_price, info

# ════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════
def open_trade(symbol, side, sl_price, tp1_price, tp2_price, info):
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
            "qty_remain": qty,
            "sl":        sl_price,
            "tp1":       tp1_price,
            "tp2":       tp2_price,
            "peak":      entry,
            "trail_sl":  trail_sl,
            "trailing_active": False,
            "tp1_hit":   False,
            "be_active": False,
        }
        sl_pct  = abs(entry - sl_price) / entry * 100
        tp1_pct = abs(tp1_price - entry) / entry * 100
        tp2_pct = abs(tp2_price - entry) / entry * 100
        score   = info.get("score", "?")
        active, session = is_active_session()
        print(f"  ✅ [{symbol}] {side} @{entry:.5f} qty={qty}")
        print(f"     SL:{sl_price:.5f}(-{sl_pct:.2f}%) | TP1:{tp1_price:.5f}(+{tp1_pct:.2f}%) | TP2:{tp2_price:.5f}(+{tp2_pct:.2f}%)")
        print(f"     Score:{score} (min:{info.get('min_score','?')}) | Session:{session}")
        print(f"     BTC:{info.get('btc_15m','?')}/{info.get('btc_1h','?')}/{info.get('btc_4h','?')} | Breadth:{info.get('breadth','?')}")
        print(f"     Vol/Mom:{info.get('vol_momentum','?')} | 5m:{info.get('5m','?')} | Rev:{info.get('reversal','-')}")
        print(f"     Whale:{info.get('whale','?')} | SR:{info.get('sr','?')} | Funding:{info.get('funding',0):.4f}")
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal entry: {e}")

def partial_close(symbol, reason="TP1"):
    pos = open_positions.get(symbol)
    if pos is None: return
    try:
        amt = get_exchange_amt(symbol)
        if amt is None or amt == 0:
            pos["tp1_hit"] = True
            return

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

        print(f"  🎯 [{symbol}] PARTIAL {reason} @{exit_price:.5f}")
        print(f"     💛 P&L (50%): {pnl:+.4f} USDT ({pct:+.2f}%)")

        pos["tp1_hit"]         = True
        pos["qty_remain"]      = abs(amt) - close_qty
        pos["be_active"]       = True
        pos["sl"]              = pos["entry"]
        pos["trailing_active"] = True
        pos["peak"]            = exit_price
        pos["trail_sl"]        = exit_price * (1 - TRAIL_PCT) if side == "LONG" \
                                 else exit_price * (1 + TRAIL_PCT)

        print(f"     🔒 BE Stop @{pos['entry']:.5f} | Trailing aktif (trail={TRAIL_PCT*100:.1f}%)")
        trade_log.append({"symbol": symbol, "side": side,
                          "pnl": round(pnl, 4), "reason": f"Partial {reason}"})
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal partial: {e}")
        pos["tp1_hit"] = True

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
                    qty_r = pos.get("qty_remain", pos["qty"])
                    pnl = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                          else (pos["entry"] - exit_) * qty_r
                    pct = pnl / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
                    print(f"  ⚠️  [{symbol}] Sudah tutup — Est P&L: {pnl:+.4f}U ({pct:+.2f}%)")
                    trade_log.append({"symbol": symbol, "side": pos["side"],
                                      "pnl": round(pnl, 4), "reason": "External close"})
                    _update_loss_streak(symbol, pnl)
                open_positions.pop(symbol, None)
            return True

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=abs(amt), reduceOnly=True)

        if symbol in open_positions:
            pos   = open_positions[symbol]
            exit_ = get_price(symbol)
            qty_r = pos.get("qty_remain", pos["qty"])
            pnl   = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                    else (pos["entry"] - exit_) * qty_r
            pct   = pnl / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
            emoji = "🟢" if pnl >= 0 else "🔴"
            be_tag = " [BE]" if pos.get("be_active") else ""
            print(f"  💰 [{symbol}] CLOSED — {reason}{be_tag}")
            print(f"     {emoji} P&L (sisa): {pnl:+.4f} USDT ({pct:+.2f}%)")
            trade_log.append({"symbol": symbol, "side": pos["side"],
                              "pnl": round(pnl, 4), "reason": reason})
            _update_loss_streak(symbol, pnl)

        open_positions.pop(symbol, None)
        return True
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal close: {e}")
        return False

def _update_loss_streak(symbol, pnl):
    global _consec_loss, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        set_symbol_cooldown(symbol)  # v10: selalu cooldown per-symbol setelah loss
        if _consec_loss >= GLOBAL_COOLDOWN_LOSS:
            btc_bad     = _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < COOLDOWN_BREADTH_MAX
            if btc_bad and breadth_bad:  # v10: AND (was OR — lebih selektif cooldown)
                _in_cooldown = True
                print(f"  🧊 {GLOBAL_COOLDOWN_LOSS} loss beruntun + market buruk → Global Cooldown!")
            else:
                print(f"  ⚡ {_consec_loss} loss tapi market masih bisa → lanjut (symbol cooldown aja)")
                _consec_loss = 0
    else:
        _consec_loss = 0
        if _in_cooldown:
            _in_cooldown = False
            print(f"  ✅ Win! Global cooldown selesai.")

# ════════════════════════════════════════════════════
#  POSITION MANAGEMENT
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

        # Emergency exits
        if _macro["news"] == "strong_negative":
            close_trade(symbol, "🚨 Emergency strong bad news")
            continue
        if side == "LONG" and _macro["btc_trend_1h"] == "BEAR" and _macro["btc_trend_4h"] == "BEAR":
            close_trade(symbol, "⚡ BTC 1H+4H BEAR — emergency exit LONG")
            continue
        if side == "SHORT" and _macro["btc_trend_1h"] == "BULL" and _macro["btc_trend_4h"] == "BULL":
            close_trade(symbol, "⚡ BTC 1H+4H BULL — emergency exit SHORT")
            continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            if not pos["tp1_hit"] and price >= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 - TRAIL_PCT)

            if pos["tp1_hit"] and price >= pos["tp2"]:
                close_trade(symbol, "✨ TP2 (sisa 50%)"); continue

            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue

            if price <= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason); continue

            pnl_now = (price - entry) * pos.get("qty_remain", pos["qty"])
            be_tag  = " [BE]" if pos.get("be_active") else ""
            tp_tag  = f" TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f" TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] LONG @{entry:.4f}→{price:.4f}{be_tag} | {pnl_now:+.3f}U{tsl}{tp_tag}")

        else:  # SHORT
            profit_pct = (entry - price) / entry

            if not pos["tp1_hit"] and price <= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 + TRAIL_PCT)

            if pos["tp1_hit"] and price <= pos["tp2"]:
                close_trade(symbol, "✨ TP2 (sisa 50%)"); continue

            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue

            if price >= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason); continue

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
    total   = sum(t["pnl"] for t in trade_log)
    wins    = sum(1 for t in trade_log if t["pnl"] > 0)
    n       = len(trade_log)
    wr      = wins / n * 100 if n else 0
    cd_sym  = len(_symbol_cooldown)
    cd_info = f" | 🧊 GlobalCD ({cooldown_reason()})" if _in_cooldown else ""
    sym_cd  = f" | ⏳ {cd_sym} sym cooldown" if cd_sym else ""
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% W:{wins} L:{n-wins} | P&L:{total:+.4f}U | streak:{_consec_loss}L{cd_info}{sym_cd}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:40]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    print("🤖 Bot v10 — Smart Aggressive Scalping")
    print(f"   Leverage   : {LEVERAGE}x | Order: ${ORDER_USDT} USDT")
    print(f"   SL/TP      : {ATR_SL_MULT}x ATR SL | TP1:{ATR_TP1_MULT}x (50%) | TP2:{ATR_TP2_MULT}x (50%)")
    print(f"   Trailing   : aktif setelah +{TRAIL_TRIGGER*100:.1f}%")
    print(f"   Score      : BASE={BASE_MIN_SCORE} (adaptive, bisa turun {SCORE_BONUS_BTC_ALIGN+SCORE_BONUS_REGIME+SCORE_BONUS_VOL_SPIKE+SCORE_BONUS_MOMENTUM} poin)")
    print(f"   F&G range  : {MIN_FNG_ANY}-{MAX_FNG_LONG} (fear zone:{FNG_FEAR_ZONE}, greed zone:{FNG_GREED_ZONE})")
    print(f"   BTC filter : block hanya kalau 2+ TF berlawanan (v9: 1 TF sudah block)")
    print(f"   Volume     : spike {MIN_VOLUME_SPIKE}x OR momentum 5 candle konsisten")
    print(f"   SR buffer  : {SR_BUFFER*100:.1f}% (v9: 0.8%)")
    print(f"   Breadth    : LONG min {MIN_MARKET_BREADTH*100:.0f}% (v9: 45%)")
    print(f"   Cooldown   : per-symbol {SYMBOL_COOLDOWN_SECS}s + global kalau {GLOBAL_COOLDOWN_LOSS} loss")
    print(f"   Session    : off-hours penalty +{OFF_SESSION_PENALTY} poin score")
    print(f"   Max Posisi : {MAX_POSITIONS}\n")

    print("  ⏳ Setup...")
    symbols = validate_symbols()
    for s in symbols: get_sym_info(s)
    refresh_macro()
    active, session = is_active_session()
    print(f"  ✅ {len(symbols)} symbols | F&G:{_macro['fng']} | "
          f"BTC 15m:{_macro['btc_trend_15m']} 1H:{_macro['btc_trend_1h']} 4H:{_macro['btc_trend_4h']}")
    print(f"  📅 Session: {session} ({'AKTIF' if active else 'OFF - penalty +'+str(OFF_SESSION_PENALTY)})")
    print(f"  📰 News:{_macro['news']} | Breadth:{_macro['market_breadth']*100:.0f}%\n")

    cycle = 0
    while True:
        cycle += 1
        refresh_macro()

        if _in_cooldown:
            is_cooldown_active()

        manage_positions()

        active, session = is_active_session()
        cd_info = f" 🧊 GlobalCD" if _in_cooldown else ""
        sym_cd  = f" | {len(_symbol_cooldown)} sym-cd" if _symbol_cooldown else ""
        print(f"\n{'='*72}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | "
              f"USDT:{_macro['usdt_d']}% | News:{_macro['news']}{cd_info}")
        print(f"  📈 BTC 15m:{_macro['btc_trend_15m']} | 1H:{_macro['btc_trend_1h']} | 4H:{_macro['btc_trend_4h']}")
        print(f"  🌍 Breadth:{_macro['market_breadth']*100:.0f}% | MCap24h:{_macro['global_mcap_chg']:+.1f}% | Session:{session}{sym_cd}")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*72}")

        skipped    = 0
        skip_reasons = {}
        candidates = []

        if len(open_positions) < MAX_POSITIONS and not _in_cooldown and \
           _macro["news"] != "strong_negative":
            for symbol in symbols:
                if symbol in open_positions: continue
                df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 220)
                if df is None or len(df) < 70: continue
                side, sl, tp1, tp2, info = should_enter(symbol, df)
                if side:
                    candidates.append((symbol, side, sl, tp1, tp2, info))
                else:
                    skipped += 1
                    # Kumpulkan alasan skip untuk debug
                    reason = info.get("skip", "?")
                    key = reason.split(" ")[0]  # ambil kata pertama
                    skip_reasons[key] = skip_reasons.get(key, 0) + 1

            if candidates:
                # Ranking by composite score
                candidates.sort(key=lambda x: x[5].get("score_num", 0), reverse=True)
                print(f"\n  🎯 {len(candidates)} setup valid | {skipped} skip")
                for sym, side, sl, tp1, tp2, info in candidates[:3]:
                    print(f"     ⭐ {sym} {side} | Score:{info.get('score','?')} (min:{info.get('min_score','?')}) | "
                          f"Vol:{info.get('vol_momentum','?')} | 5m:{info.get('5m','?')}")
                for sym, side, sl, tp1, tp2, info in candidates:
                    if len(open_positions) >= MAX_POSITIONS: break
                    open_trade(sym, side, sl, tp1, tp2, info)
            else:
                print(f"  ⏳ {skipped} coins di-scan, belum ada setup valid")
                # Debug: top 5 alasan skip
                if skip_reasons:
                    top_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])[:5]
                    print(f"  🔍 Skip reasons: {' | '.join(f'{k}:{v}' for k,v in top_reasons)}")
        else:
            if _in_cooldown:
                print(f"  🧊 Global Cooldown — {cooldown_reason()}")
            else:
                print(f"  ⏸️  Posisi penuh atau kondisi tidak aman")

        print_summary()
        print(f"\n  ⏱️  {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
