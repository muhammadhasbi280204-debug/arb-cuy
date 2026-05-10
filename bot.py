"""
Bot Trading v11 — High-Precision Scalping
==========================================
PERUBAHAN UTAMA dari v10:

═══ 1. FEE-AWARE SL/TP ═══════════════════════════════════════════
  v10: SL/TP tidak memperhitungkan fee → profit semu
  v11: Setiap SL/TP diperhitungkan fee taker 0.04% per sisi
       SL minimum = ATR * mult + 2x fee (agar tidak langsung minus setelah fee)
       TP minimum = SL * RR_RATIO (default 1.5) + 2x fee

═══ 2. COMPOSITE SCORE DIPERBAIKI ════════════════════════════════
  v10: Bobot arbitrary, normalisasi salah (max_possible * 2 tanpa alasan)
  v11: Bobot dinaikkan untuk indikator dengan confirmed edge:
       - MACD histogram cross: sinyal paling reliable di crypto scalping
       - EMA stack: trend following terbukti
       - RSI divergence: presisi lebih tinggi (bukan sekedar overbought/oversold)
       - Volume relatif: konfirmasi momentum
       Normalisasi benar: score / max_possible * 100

═══ 3. SIGNAL QUALITY FILTER ═════════════════════════════════════
  v11 BARU: Sebelum entry, cek konfluensi minimum:
       - Harus ada minimal 3 dari 5 sinyal utama (MACD + EMA + RSI + Vol + OB)
       - Tidak cukup score tinggi saja — harus ada BREADTH sinyal

═══ 4. DYNAMIC POSITION SIZING ══════════════════════════════════
  v10: Selalu ORDER_USDT * LEVERAGE
  v11: Size dikurangi kalau:
       - Score rendah (dekat minimum) → size 70%
       - F&G di fear/greed extreme → size 80%
       - Consecutive loss 2 → size 60% (recovery mode)

═══ 5. TOP 100 BINANCE FUTURES COINS ════════════════════════════
  v10: 28 coin
  v11: 100 coin — otomatis filter yang tidak tersedia di testnet/real

═══ 6. IMPROVED TRAILING STOP ════════════════════════════════════
  v10: Trail flat % dari peak
  v11: ATR-based trailing — trail lebih lebar di volatile market,
       lebih ketat saat approaching TP2

═══ 7. TIME-BASED FORCED EXIT ════════════════════════════════════
  v11 BARU: Posisi yang tidak mencapai TP1 dalam MAX_HOLD_MINUTES
       akan di-close paksa untuk hindari overnight/drawdown panjang

═══ 8. PARALLEL SCAN (threading) ═════════════════════════════════
  v10: Serial scan semua coin satu per satu (lambat)
  v11: ThreadPoolExecutor — scan 5 coin sekaligus, lebih cepat

═══ DISCLAIMER ══════════════════════════════════════════════════
  Trading crypto futures dengan leverage mengandung risiko kehilangan
  seluruh modal. Bot ini bukan jaminan profit. Selalu gunakan testnet
  dulu minimal 2-4 minggu sebelum live trading. Gunakan modal yang
  siap hilang sepenuhnya.
"""

import os, time, math, json, requests, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
ORDER_USDT            = 55          # modal per trade (sebelum leverage)
TAKER_FEE             = 0.0004      # 0.04% per sisi (Binance futures taker)
ROUND_TRIP_FEE        = TAKER_FEE * 2  # entry + exit

# SL/TP — fee-aware
ATR_SL_MULT           = 1.6        # SL = 1.6x ATR + fee (lebih sedikit ruang palsu)
RR_RATIO              = 1.6        # TP1 minimal 1.6x SL (supaya profit setelah fee)
ATR_TP1_MULT          = ATR_SL_MULT * RR_RATIO  # ~2.56x ATR
ATR_TP2_MULT          = ATR_SL_MULT * RR_RATIO * 2.2  # ~5.6x ATR

# Trailing
TRAIL_TRIGGER         = 0.004      # aktif setelah +0.4% profit
ATR_TRAIL_MULT        = 0.8        # trail = 0.8x ATR dari peak (dinamis)

# Position & risk
MAX_POSITIONS         = 3
MAX_HOLD_MINUTES      = 120        # paksa exit kalau nyangkut > 2 jam
MAX_CONSEC_LOSS       = 2          # sebelum size dikurangi
SCAN_INTERVAL         = 40         # detik

# Score thresholds
BASE_MIN_SCORE        = 55         # lebih ketat dari v10 (was 52)
MIN_CONFLUENCE        = 3          # minimal 3/5 sinyal utama harus agree
SCORE_BONUS_BTC_ALIGN = 8
OFF_SESSION_PENALTY   = 10

# Macro filters
MIN_FNG_ANY           = 15
MAX_FNG_LONG          = 88
FNG_FEAR_ZONE         = 38
FNG_GREED_ZONE        = 78
MIN_MARKET_BREADTH    = 0.32
SR_BUFFER             = 0.004
MIN_VOLUME_SPIKE      = 1.25
USDT_RISK_OFF_DELTA   = 0.05

# Cooldown
SYMBOL_COOLDOWN_SECS  = 360
GLOBAL_COOLDOWN_LOSS  = 4
COOLDOWN_BTC_BAD      = {"BEAR"}
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL", "SIDEWAYS"}
COOLDOWN_BREADTH_MIN  = 0.38

# Session filter (UTC)
ACTIVE_SESSIONS = {
    "ASIA_OPEN":  (0, 4),
    "EU_OPEN":    (7, 11),
    "NY_OPEN":    (13, 17),
    "OVERLAP":    (13, 15),
}

# ══ COMPOSITE SCORE WEIGHTS (diperbaiki v11) ════════
# Bobot lebih tinggi untuk sinyal dengan confirmed edge di crypto
SCORE_WEIGHTS = {
    "macd_cross":   22,   # MACD histogram cross — sinyal terkuat
    "ema_stack":    18,   # EMA 9/21/50 alignment
    "rsi":          15,   # RSI dengan divergence check
    "volume":       15,   # Volume relatif terhadap MA20
    "ob_imbalance": 12,   # Order book imbalance
    "stoch_cross":  10,   # Stochastic cross di oversold/overbought
    "cum_delta":     8,   # Cumulative delta (taker buy/sell)
}
# Total max = 100 → normalisasi benar

# ══ TOP 100 BINANCE FUTURES COINS ══════════════════
SYMBOLS = [
    # Large cap
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","TRXUSDT","DOTUSDT",
    # Mid cap tier 1
    "LINKUSDT","MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT",
    "ETCUSDT","XLMUSDT","NEARUSDT","APTUSDT","ARBUSDT",
    "OPUSDT","INJUSDT","SUIUSDT","TIAUSDT","AAVEUSDT",
    "RUNEUSDT","FILUSDT","LDOUSDT","MKRUSDT","SNXUSDT",
    # Mid cap tier 2
    "1000PEPEUSDT","WIFUSDT","JUPUSDT","SEIUSDT","PYTHUSDT",
    "WLDUSDT","STRKUSDT","ALTUSDT","JUPUSDT","DYMUSDT",
    "RONINUSDT","PIXELUSDT","PORTALUSDT","ACEUSDT","XAIUSDT",
    "MANTAUSDT","ZETAUSDT","AIUSDT","WUSDT","BOMEUSDT",
    # Established alts
    "SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","ENJUSDT",
    "CHZUSDT","FLOWUSDT","IMXUSDT","LOOMUSDT","SKLUSDT",
    "CELOUSDT","ZRXUSDT","BANDUSDT","STORJUSDT","CRVUSDT",
    "COMPUSDT","YFIUSDT","SUSHIUSDT","1INCHUSDT","BALUSDT",
    # DeFi & Layer 2
    "GMXUSDT","DYDXUSDT","PERPUSDT","BLURUSDT","MAGICUSDT",
    "RDNTUSDT","PENDLEUSDT","UMAUSDT","LITUSDT","LQTYUSDT",
    # Meme & high vol
    "FLOKIUSDT","1000SHIBUSDT","BONKUSDT","MEMEUSDT","DOGUSDT",
    # Others
    "FETUSDT","AGIXUSDT","OCEANUSDT","RNDRUSDT","THETAUSDT",
    "VETUSDT","ZILUSDT","IOSTUSDT","ONEUSDT","ANKRUSDT",
    "CELRUSDT","REEFUSDT","SFPUSDT","CKBUSDT","ACAUSDT",
    "ROSEUSDT","WOOUSDT","APEUSDT","GALUSDT","HOOKUSDT",
    "MAGICUSDT","HIGHUSDT","MINAUSDT","CFXUSDT","STXUSDT",
    "KASUSDT","ORDIUSDT","SATSUSDT","TAOUSDT","JTOUSDT",
]
# Deduplicate
SYMBOLS = list(dict.fromkeys(SYMBOLS))

# ════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════
open_positions   = {}
trade_log        = []
_last_candle     = {}
_consec_loss     = 0
_in_cooldown     = False
_symbol_cooldown = {}
_scan_lock       = threading.Lock()

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
    """v11: fraction untuk dynamic position sizing."""
    info = get_sym_info(symbol)
    raw  = (ORDER_USDT * LEVERAGE * fraction) / price
    return max(round_step(raw, info["step"]), info["minQty"])

def get_position_fraction(score, min_score, fng, consec_loss):
    """
    v11 BARU: Dynamic position sizing berdasarkan kualitas setup.
    Setup bagus = full size. Setup pas-pasan = size dikurangi.
    """
    fraction = 1.0

    # Score dekat minimum → kurangi size
    score_margin = score - min_score
    if score_margin < 8:
        fraction *= 0.70
    elif score_margin < 15:
        fraction *= 0.85

    # F&G extreme → kurangi size
    if fng < FNG_FEAR_ZONE or fng > FNG_GREED_ZONE:
        fraction *= 0.80

    # Recovery mode setelah loss beruntun
    if consec_loss >= 2:
        fraction *= 0.60
    elif consec_loss >= 1:
        fraction *= 0.80

    return max(fraction, 0.50)  # minimum 50% size

def set_leverage(symbol):
    try: client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except: pass

def get_price(symbol):
    try: return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except: return 0.0

def validate_symbols():
    try:
        valid  = {s["symbol"] for s in client.futures_exchange_info()["symbols"]
                  if s["status"] == "TRADING"}
        result = list(dict.fromkeys([s for s in SYMBOLS if s in valid]))
        print(f"  ✅ {len(result)}/{len(SYMBOLS)} symbols valid di exchange")
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
    utc_hour = time.gmtime().tm_hour
    for name, (start, end) in ACTIVE_SESSIONS.items():
        if start <= utc_hour < end:
            return True, name
    return False, "OFF"

# ════════════════════════════════════════════════════
#  OHLCV
# ════════════════════════════════════════════════════
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

# ════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE
# ════════════════════════════════════════════════════
def get_sr_levels(symbol, lookback=24):
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_4HOUR, lookback + 5)
    if df is None or len(df) < 10:
        return {"resistance": [], "support": []}
    highs, lows = df["high"].values, df["low"].values
    resistance, support = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support.append(lows[i])
    return {"resistance": sorted(resistance, reverse=True)[:4],
            "support":    sorted(support)[:4]}

def check_sr_clear(symbol, price, direction):
    sr = get_sr_levels(symbol)
    if direction == "LONG":
        nearby = [r for r in sr["resistance"] if r > price]
        if nearby:
            nearest  = min(nearby)
            gap_pct  = (nearest - price) / price
            if gap_pct < SR_BUFFER:
                return False, f"Resistance {nearest:.4f} ({gap_pct*100:.2f}%)"
    else:
        nearby = [s for s in sr["support"] if s < price]
        if nearby:
            nearest = max(nearby)
            gap_pct = (price - nearest) / price
            if gap_pct < SR_BUFFER:
                return False, f"Support {nearest:.4f} ({gap_pct*100:.2f}%)"
    return True, ""

# ════════════════════════════════════════════════════
#  5m SCALP CONFIRMATION
# ════════════════════════════════════════════════════
def get_5m_confirmation(symbol, direction):
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_5MINUTE, 20)
    if df is None or len(df) < 15:
        return True, "no data"
    df   = df.iloc[:-1].copy()
    c    = df["close"]
    ema9 = ta.trend.EMAIndicator(c, 9).ema_indicator()
    last = df.iloc[-1]
    last3 = df.tail(3)
    up_c  = sum(1 for _, r in last3.iterrows() if r["close"] > r["open"])
    dn_c  = sum(1 for _, r in last3.iterrows() if r["close"] < r["open"])

    if direction == "LONG":
        above = last["close"] > ema9.iloc[-1]
        mom   = up_c >= 2
        if above and mom:   return True, f"5m✅ {up_c}/3 green"
        elif above or mom:  return True, f"5m✓ partial"
        return False, f"5m❌ {up_c}/3 green"
    else:
        below = last["close"] < ema9.iloc[-1]
        mom   = dn_c >= 2
        if below and mom:   return True, f"5m✅ {dn_c}/3 red"
        elif below or mom:  return True, f"5m✓ partial"
        return False, f"5m❌ {dn_c}/3 red"

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
    if df is None or len(df) < 55: return "UNKNOWN"
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
            _macro["global_mcap_chg"] = round(d["data"].get("market_cap_change_percentage_24h_usd", 0), 2)
            _macro["last_dom"]  = now
        except: pass

    if now - _macro["last_news"] > 60:
        try:
            data = requests.get(
                "https://cryptopanic.com/api/v1/posts/?auth_token=demo&public=true&currencies=BTC",
                timeout=5).json()
            neg_kw_strong = ["crash","hack","ban","fraud","collapse","seized","scam","exploit","rug"]
            neg_kw_mild   = ["bear","fear","lawsuit","dump","warning","plunge","sell-off","decline","fud"]
            pos_kw_strong = ["institutional","ath","approved","record","bullish","rally","surge","etf"]
            pos_kw_mild   = ["adoption","breakout","buy","launched","partnership","soar","recovery"]
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

    if now - _macro["last_btc"] > 40:
        try:
            df15 = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_15MINUTE, 60)
            df1h = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_1HOUR, 60)
            df4h = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_4HOUR, 60)
            _macro["btc_trend_15m"] = _calc_btc_trend(df15)
            _macro["btc_trend_1h"]  = _calc_btc_trend(df1h)
            _macro["btc_trend_4h"]  = _calc_btc_trend(df4h)
            _macro["last_btc"]      = now
        except: pass

    if now - _macro["last_breadth"] > 300:
        try:
            # Sample 20 coin (lebih representatif dari v10 yang 15)
            bullish = 0
            sample  = SYMBOLS[:20]
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
#  ADAPTIVE SCORE THRESHOLD
# ════════════════════════════════════════════════════
BULL_TRENDS = {"BULL", "MILD_BULL"}
BEAR_TRENDS = {"BEAR", "MILD_BEAR"}

# ════════════════════════════════════════════════════
#  MARKET CONTEXT — tentukan allowed directions & kondisi
#  SEBELUM scan dimulai, bukan saat per-symbol
# ════════════════════════════════════════════════════
def get_market_context():
    """
    v12: Evaluasi kondisi macro SEKALI per siklus → tentukan:
      - allowed_directions: set arah yang boleh dimasuki
      - quality_gate: "PREMIUM" / "NORMAL" / "SKIP"
      - reason: penjelasan singkat

    Logika:
      BTC 4H+1H BULL  → hanya LONG diperbolehkan (SHORT = forbidden)
      BTC 4H+1H BEAR  → hanya SHORT diperbolehkan
      BTC mixed       → keduanya boleh tapi dengan score lebih tinggi
      Breadth <20%    → SKIP semua (market terlalu lemah, noise dominan)
      Breadth 20-32%  → hanya SHORT premium
      Session OFF     → naikkan threshold, bukan block total
    """
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]
    breadth = _macro["market_breadth"]

    bull_tf = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
    bear_tf = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)

    # ── Breadth extreme — skip semua ──────────────────
    # Dari log: breadth 15% = market hampir semua coin turun
    # Tidak ada gunanya entry LONG, SHORT pun noise
    if breadth < 0.18:
        return set(), "SKIP", f"Breadth ekstrem rendah {breadth*100:.0f}% (<18%) — semua skip"

    # ── BTC 4H+1H keduanya BULL → larang SHORT ────────
    # Ini akar masalah dari log kamu: SHORT MANA+INJ kena exit
    if t4h in BULL_TRENDS and t1h in BULL_TRENDS:
        if breadth >= MIN_MARKET_BREADTH:
            return {"LONG"}, "PREMIUM", f"BTC 4H+1H BULL ({t4h}/{t1h}) — LONG only"
        else:
            return {"LONG"}, "NORMAL", f"BTC 4H+1H BULL tapi breadth {breadth*100:.0f}%"

    # ── BTC 4H+1H keduanya BEAR → larang LONG ─────────
    if t4h in BEAR_TRENDS and t1h in BEAR_TRENDS:
        if breadth <= 0.55:
            return {"SHORT"}, "PREMIUM", f"BTC 4H+1H BEAR ({t4h}/{t1h}) — SHORT only"
        else:
            return {"SHORT"}, "NORMAL", f"BTC 4H+1H BEAR tapi breadth tinggi {breadth*100:.0f}%"

    # ── BTC mixed (contoh: 4H BULL tapi 1H BEAR) ──────
    # Kondisi choppy — izinkan keduanya tapi score lebih ketat
    if bull_tf >= 2:
        return {"LONG", "SHORT"}, "NORMAL", f"BTC mixed bullish {bull_tf}/3 TF"
    if bear_tf >= 2:
        return {"LONG", "SHORT"}, "NORMAL", f"BTC mixed bearish {bear_tf}/3 TF"

    # ── BTC benar-benar sideways / semua MILD/SIDEWAYS ─
    # Breadth jadi penentu utama
    if breadth < MIN_MARKET_BREADTH:
        return {"SHORT"}, "NORMAL", f"BTC sideways + breadth rendah {breadth*100:.0f}%"
    if breadth > 0.68:
        return {"LONG"}, "NORMAL", f"BTC sideways + breadth tinggi {breadth*100:.0f}%"

    return {"LONG", "SHORT"}, "NORMAL", f"BTC sideways, breadth {breadth*100:.0f}% netral"

def get_adaptive_min_score(direction, quality_gate="NORMAL"):
    """
    v12: Skor minimum menyesuaikan quality_gate dari get_market_context().
    PREMIUM  = macro sangat mendukung → threshold turun lebih banyak
    NORMAL   = macro mixed → threshold standar
    """
    min_score = BASE_MIN_SCORE
    bonuses   = []
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
    else:
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)

    # Bonus BTC alignment — lebih besar kalau PREMIUM
    if btc_align >= 3:
        bonus = SCORE_BONUS_BTC_ALIGN + (3 if quality_gate == "PREMIUM" else 0)
        min_score -= bonus
        bonuses.append(f"BTC3TF-{bonus}")
    elif btc_align >= 2:
        bonus = SCORE_BONUS_BTC_ALIGN // 2
        min_score -= bonus
        bonuses.append(f"BTC2TF-{bonus}")
    else:
        # Tidak ada alignment = naikkan threshold (trading melawan macro)
        min_score += 8
        bonuses.append("BTC_noalign+8")

    # Session
    active, session = is_active_session()
    if session == "OVERLAP":
        min_score -= 5; bonuses.append("OVERLAP-5")
    elif active:
        min_score -= 2; bonuses.append(f"{session}-2")
    else:
        min_score += OFF_SESSION_PENALTY; bonuses.append(f"OFF+{OFF_SESSION_PENALTY}")

    # F&G
    fng = _macro["fng"]
    if direction == "LONG" and fng < FNG_FEAR_ZONE:
        min_score += 6; bonuses.append("FNG_FEAR+6")
    elif direction == "SHORT" and fng < FNG_FEAR_ZONE:
        min_score -= 4; bonuses.append("FNG_FEAR_SHORT-4")
    elif direction == "LONG" and fng > FNG_GREED_ZONE:
        min_score -= 3; bonuses.append("FNG_GREED-3")

    # Breadth penalty tambahan
    breadth = _macro["market_breadth"]
    if breadth < 0.30 and direction == "LONG":
        min_score += 10; bonuses.append(f"LOW_BREADTH+10")
    elif breadth > 0.70 and direction == "SHORT":
        min_score += 10; bonuses.append(f"HIGH_BREADTH+10")

    return max(min_score, 42), bonuses

def btc_multi_tf_ok_for(direction):
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]
    if direction == "LONG":
        if t4h in BEAR_TRENDS and t1h in BEAR_TRENDS:
            return False, f"BTC 4H={t4h}+1H={t1h} bearish"
        if sum(1 for t in [t15,t1h,t4h] if t in BEAR_TRENDS) >= 3:
            return False, f"BTC semua TF bearish"
    elif direction == "SHORT":
        if t4h in BULL_TRENDS and t1h in BULL_TRENDS:
            return False, f"BTC 4H={t4h}+1H={t1h} bullish"
        if sum(1 for t in [t15,t1h,t4h] if t in BULL_TRENDS) >= 3:
            return False, f"BTC semua TF bullish"
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
#  TECHNICAL ANALYSIS
# ════════════════════════════════════════════════════
def run_ta(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["rsi"]        = ta.momentum.RSIIndicator(c, 14).rsi()
    df["rsi_fast"]   = ta.momentum.RSIIndicator(c, 7).rsi()
    macd             = ta.trend.MACD(c, 12, 26, 9)
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
    return df

def calc_composite_score(df, regime, ob_imb, cum_d):
    """
    v11: Score lebih presisi.
    - Normalisasi benar: skor / max_possible * 100
    - Bobot mencerminkan kehandalan sinyal
    - Return: (direction, score_0_100, breakdown, confluence_count)
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]
    W    = SCORE_WEIGHTS
    breakdown = {}
    long_pts = short_pts = 0.0
    long_signals = short_signals = 0   # untuk confluence check

    # ── 1. MACD Cross (bobot 22) ─────────────────────
    # Bukan sekedar histogram positif — tapi CROSS yang baru terjadi
    hist_now  = last["macd_hist"]
    hist_prev = prev["macd_hist"]
    macd_cross_long  = hist_now > 0 and hist_prev <= 0  # baru cross ke atas
    macd_cross_short = hist_now < 0 and hist_prev >= 0  # baru cross ke bawah
    macd_trend_long  = hist_now > 0 and hist_now > hist_prev  # trend naik
    macd_trend_short = hist_now < 0 and hist_now < hist_prev  # trend turun

    if macd_cross_long:
        long_pts += W["macd_cross"]; long_signals += 1
        breakdown["macd"] = f"+{W['macd_cross']}L(cross)"
    elif macd_trend_long:
        pts = W["macd_cross"] * 0.65
        long_pts += pts; long_signals += 1
        breakdown["macd"] = f"+{pts:.0f}L(trend)"
    elif macd_cross_short:
        short_pts += W["macd_cross"]; short_signals += 1
        breakdown["macd"] = f"+{W['macd_cross']}S(cross)"
    elif macd_trend_short:
        pts = W["macd_cross"] * 0.65
        short_pts += pts; short_signals += 1
        breakdown["macd"] = f"+{pts:.0f}S(trend)"
    else:
        breakdown["macd"] = "0(neutral)"

    # ── 2. EMA Stack (bobot 18) ──────────────────────
    e9, e21, e50 = last["ema9"], last["ema21"], last["ema50"]
    price = last["close"]
    if e9 > e21 > e50 and price > e9:
        long_pts += W["ema_stack"]; long_signals += 1
        breakdown["ema"] = f"+{W['ema_stack']}L(full)"
    elif e9 < e21 < e50 and price < e9:
        short_pts += W["ema_stack"]; short_signals += 1
        breakdown["ema"] = f"+{W['ema_stack']}S(full)"
    elif e9 > e21 and price > e21:
        pts = W["ema_stack"] * 0.55
        long_pts += pts
        breakdown["ema"] = f"+{pts:.0f}L(partial)"
    elif e9 < e21 and price < e21:
        pts = W["ema_stack"] * 0.55
        short_pts += pts
        breakdown["ema"] = f"+{pts:.0f}S(partial)"
    else:
        breakdown["ema"] = "0"

    # ── 3. RSI (bobot 15) ────────────────────────────
    rsi      = last["rsi"]
    rsi_prev = prev["rsi"]
    if rsi < 38 and rsi > rsi_prev:      # oversold tapi mulai naik = reversal
        pts = W["rsi"] if rsi < 30 else W["rsi"] * 0.65
        long_pts += pts; long_signals += 1
        breakdown["rsi"] = f"+{pts:.0f}L({rsi:.0f}↑)"
    elif rsi > 62 and rsi < rsi_prev:    # overbought tapi mulai turun
        pts = W["rsi"] if rsi > 70 else W["rsi"] * 0.65
        short_pts += pts; short_signals += 1
        breakdown["rsi"] = f"+{pts:.0f}S({rsi:.0f}↓)"
    else:
        breakdown["rsi"] = f"0({rsi:.0f})"

    # ── 4. Volume (bobot 15) ─────────────────────────
    vr = last["vol_ratio"]
    br = last["buy_ratio"]
    if vr >= MIN_VOLUME_SPIKE:
        if last["close"] > last["open"] and br > 0.53:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 1.8)
            long_pts += pts; long_signals += 1
            breakdown["vol"] = f"+{pts:.0f}L({vr:.1f}x)"
        elif last["close"] < last["open"] and br < 0.47:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 1.8)
            short_pts += pts; short_signals += 1
            breakdown["vol"] = f"+{pts:.0f}S({vr:.1f}x)"
        else:
            breakdown["vol"] = f"spike({vr:.1f}x)ambig"
    elif vr >= 0.85 and br > 0.55 and last["close"] > last["open"]:
        long_pts += W["volume"] * 0.35
        breakdown["vol"] = f"+{W['volume']*0.35:.0f}L(weak)"
    elif vr >= 0.85 and br < 0.45 and last["close"] < last["open"]:
        short_pts += W["volume"] * 0.35
        breakdown["vol"] = f"+{W['volume']*0.35:.0f}S(weak)"
    else:
        breakdown["vol"] = f"0({vr:.1f}x)"

    # ── 5. Order Book Imbalance (bobot 12) ───────────
    if ob_imb > 0.10:
        pts = W["ob_imbalance"] * min(ob_imb / 0.10, 1.5)
        long_pts += pts; long_signals += 1
        breakdown["ob"] = f"+{pts:.0f}L({ob_imb:+.2f})"
    elif ob_imb < -0.10:
        pts = W["ob_imbalance"] * min(abs(ob_imb) / 0.10, 1.5)
        short_pts += pts; short_signals += 1
        breakdown["ob"] = f"+{pts:.0f}S({ob_imb:+.2f})"
    else:
        breakdown["ob"] = f"neutral({ob_imb:+.2f})"

    # ── 6. Stochastic Cross (bobot 10) ───────────────
    k, d    = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    stoch_cross_long  = k > d and pk <= pd_ and k < 35   # cross di oversold
    stoch_cross_short = k < d and pk >= pd_ and k > 65   # cross di overbought
    if stoch_cross_long:
        short_signals += 1  # dihitung tapi sebagai long signal
        long_pts += W["stoch_cross"]; long_signals += 1
        breakdown["stoch"] = f"+{W['stoch_cross']}L(cross {k:.0f})"
    elif stoch_cross_short:
        short_pts += W["stoch_cross"]; short_signals += 1
        breakdown["stoch"] = f"+{W['stoch_cross']}S(cross {k:.0f})"
    elif k < 25:
        long_pts += W["stoch_cross"] * 0.4
        breakdown["stoch"] = f"+{W['stoch_cross']*0.4:.0f}L(deep {k:.0f})"
    elif k > 75:
        short_pts += W["stoch_cross"] * 0.4
        breakdown["stoch"] = f"+{W['stoch_cross']*0.4:.0f}S(deep {k:.0f})"
    else:
        breakdown["stoch"] = f"0({k:.0f})"

    # ── 7. Cumulative Delta (bobot 8) ─────────────────
    if cum_d > 0.10:
        long_pts += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}L"
    elif cum_d < -0.10:
        short_pts += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}S"
    else:
        breakdown["delta"] = f"0({cum_d:+.2f})"

    # ── Regime multiplier ─────────────────────────────
    if regime == "BULL":
        long_pts  *= 1.12
        short_pts *= 0.82
    elif regime == "BEAR":
        short_pts *= 1.12
        long_pts  *= 0.82

    # ── Normalisasi yang benar ─────────────────────────
    max_possible = sum(W.values())  # = 100 secara desain
    long_pct  = min(long_pts  / max_possible * 100, 100)
    short_pct = min(short_pts / max_possible * 100, 100)

    # Gap minimal 10 untuk arah yang jelas
    if long_pct > short_pct + 10:
        return "LONG",  long_pct,  breakdown, long_signals
    if short_pct > long_pct + 10:
        return "SHORT", short_pct, breakdown, short_signals
    return "NONE", max(long_pct, short_pct), breakdown, 0

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
    recent = recent.copy()
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
#  FEE-AWARE SL/TP CALCULATOR (v11 BARU)
# ════════════════════════════════════════════════════
def calc_fee_aware_sltp(price, direction, atr):
    """
    v11: SL dan TP memperhitungkan round-trip fee.
    Minimum SL distance = ATR * mult + fee (supaya tidak langsung rugi karena fee)
    Minimum TP = SL * RR_RATIO (supaya EV positif bahkan dengan 40% win rate)
    """
    fee_dist   = price * ROUND_TRIP_FEE  # jarak minimum untuk cover fee
    sl_dist    = max(ATR_SL_MULT * atr, fee_dist * 2.5)   # minimal 2.5x fee
    tp1_dist   = max(sl_dist * RR_RATIO, fee_dist * 3.5)  # minimal 3.5x fee
    tp2_dist   = tp1_dist * 2.2

    if direction == "LONG":
        sl_p  = round(price - sl_dist,  8)
        tp1_p = round(price + tp1_dist, 8)
        tp2_p = round(price + tp2_dist, 8)
    else:
        sl_p  = round(price + sl_dist,  8)
        tp1_p = round(price - tp1_dist, 8)
        tp2_p = round(price - tp2_dist, 8)

    sl_pct  = sl_dist  / price * 100
    tp1_pct = tp1_dist / price * 100
    rr      = tp1_dist / sl_dist

    return sl_p, tp1_p, tp2_p, sl_pct, tp1_pct, rr

# ════════════════════════════════════════════════════
#  COOLDOWN MANAGEMENT
# ════════════════════════════════════════════════════
def is_symbol_in_cooldown(symbol):
    if symbol not in _symbol_cooldown: return False
    elapsed = time.time() - _symbol_cooldown[symbol]
    if elapsed > SYMBOL_COOLDOWN_SECS:
        del _symbol_cooldown[symbol]; return False
    return True

def get_symbol_cooldown_remaining(symbol):
    if symbol not in _symbol_cooldown: return 0
    return max(0, SYMBOL_COOLDOWN_SECS - (time.time() - _symbol_cooldown[symbol]))

def set_symbol_cooldown(symbol):
    _symbol_cooldown[symbol] = time.time()
    print(f"  🧊 [{symbol}] Symbol cooldown {SYMBOL_COOLDOWN_SECS}s")

def check_global_cooldown_recover():
    btc_ok     = _macro["btc_trend_15m"] in COOLDOWN_BTC_RECOVER
    breadth_ok = _macro["market_breadth"] >= COOLDOWN_BREADTH_MIN
    return btc_ok or breadth_ok

def is_cooldown_active():
    global _in_cooldown
    if not _in_cooldown: return False
    if check_global_cooldown_recover():
        _in_cooldown = False
        print(f"  ✅ Global cooldown selesai! BTC:{_macro['btc_trend_15m']}")
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
#  MASTER ENTRY FILTER (v12)
# ════════════════════════════════════════════════════
def should_enter(symbol, df, allowed_dirs=None, quality_gate="NORMAL"):
    """
    v12: Terima allowed_dirs & quality_gate dari get_market_context()
    yang sudah dievaluasi SEKALI di awal siklus — tidak perlu re-evaluasi
    BTC macro per coin.
    """
    if allowed_dirs is None:
        allowed_dirs = {"LONG", "SHORT"}

    # ── 0. Symbol cooldown ────────────────────────────
    if is_symbol_in_cooldown(symbol):
        remaining = get_symbol_cooldown_remaining(symbol)
        return None, 0, 0, 0, {"skip": f"⏳ Cooldown {remaining:.0f}s"}

    # ── 1. Global cooldown ────────────────────────────
    if is_cooldown_active():
        return None, 0, 0, 0, {"skip": f"🧊 Global cooldown ({cooldown_reason()})"}

    # ── 2. Macro hard blocks ──────────────────────────
    fng     = _macro["fng"]
    news    = _macro["news"]
    usdt_up = _macro["usdt_d"] > _macro["usdt_prev"] + USDT_RISK_OFF_DELTA

    if fng < MIN_FNG_ANY:
        return None, 0, 0, 0, {"skip": f"F&G ekstrem ({fng})"}
    if news == "strong_negative":
        return None, 0, 0, 0, {"skip": "News strong_negative"}
    if usdt_up:
        return None, 0, 0, 0, {"skip": f"USDT.D risk-off"}

    # ── 3. Candle timing ──────────────────────────────
    prev_candle_time = int(df["time"].iloc[-2])
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 80:
        return None, 0, 0, 0, {"skip": "Data kurang"}
    if _last_candle.get(symbol) == prev_candle_time:
        return None, 0, 0, 0, {"skip": "Sudah dianalisa"}

    # ── 4. TA ─────────────────────────────────────────
    df_closed = run_ta(df_closed)
    ob_imb    = get_ob_imbalance(symbol)
    cum_d     = get_cum_delta(df_closed)
    regime    = get_regime(symbol)
    funding   = get_funding(symbol)

    ta_dir, score, breakdown, confluence = calc_composite_score(
        df_closed, regime, ob_imb, cum_d)

    if ta_dir == "NONE":
        return None, 0, 0, 0, {"skip": f"Arah tidak jelas ({score:.1f})"}

    # ── 5. Direction vs market context (v12 — KUNCI) ──
    # Cek ini SEBELUM score threshold — tidak buang waktu hitung score
    # kalau arahnya sudah dilarang oleh macro
    if ta_dir not in allowed_dirs:
        return None, 0, 0, 0, {"skip": f"Dir {ta_dir} dilarang (macro:{'/'.join(allowed_dirs)})"}

    # ── 6. Confluence check ───────────────────────────
    if confluence < MIN_CONFLUENCE:
        return None, 0, 0, 0, {"skip": f"Confluence rendah ({confluence}/{MIN_CONFLUENCE})"}

    # ── 7. Adaptive min score (pakai quality_gate) ────
    min_score, score_ctx = get_adaptive_min_score(ta_dir, quality_gate)
    if score < min_score:
        return None, 0, 0, 0, {"skip": f"Score {score:.1f} < min {min_score}"}

    # ── 8. F&G directional check ──────────────────────
    if ta_dir == "LONG" and fng > MAX_FNG_LONG:
        return None, 0, 0, 0, {"skip": f"F&G euphoria ({fng})"}
    if ta_dir == "LONG" and fng < MIN_FNG_ANY + 10:
        if score < min_score + 12:
            return None, 0, 0, 0, {"skip": f"Fear zone F&G={fng}, score kurang"}

    # ── 9. Market breadth ─────────────────────────────
    breadth = _macro["market_breadth"]
    if ta_dir == "LONG" and breadth < MIN_MARKET_BREADTH:
        return None, 0, 0, 0, {"skip": f"Breadth {breadth*100:.0f}%"}
    if ta_dir == "SHORT" and breadth > 0.72:
        return None, 0, 0, 0, {"skip": f"Breadth terlalu tinggi untuk SHORT"}

    # ── 10. Regime vs direction ───────────────────────
    # v12: counter-trend threshold lebih ketat (+22, was +18)
    if ta_dir == "LONG" and regime == "BEAR" and score < min_score + 22:
        return None, 0, 0, 0, {"skip": "Counter-trend LONG di BEAR, butuh score tinggi"}
    if ta_dir == "SHORT" and regime == "BULL" and score < min_score + 22:
        return None, 0, 0, 0, {"skip": "Counter-trend SHORT di BULL, butuh score tinggi"}

    # ── 11. Volume check ──────────────────────────────
    recent = df_closed.iloc[-4:-1]
    vol_ok = False
    for _, row in recent.iterrows():
        vr = row["vol_ratio"]
        br = row["buy_ratio"]
        if vr >= MIN_VOLUME_SPIKE:
            if ta_dir == "LONG" and row["close"] > row["open"] and br > 0.52:
                vol_ok = True; break
            if ta_dir == "SHORT" and row["close"] < row["open"] and br < 0.48:
                vol_ok = True; break
    if not vol_ok:
        recent5 = df_closed.tail(6).iloc[:-1]
        if ta_dir == "LONG":
            up_c = sum(1 for _, r in recent5.iterrows() if r["close"] > r["open"])
            if up_c >= 4: vol_ok = True
        else:
            dn_c = sum(1 for _, r in recent5.iterrows() if r["close"] < r["open"])
            if dn_c >= 4: vol_ok = True
    if not vol_ok:
        return None, 0, 0, 0, {"skip": "Volume/momentum lemah"}

    # ── 12. Whale filter ──────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    if ta_dir == "LONG" and whale_dir == "sell_whale":
        return None, 0, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, 0, {"skip": "Whale buy aktif"}

    # ── 13. Funding extreme ───────────────────────────
    if ta_dir == "LONG" and funding > 0.10:
        return None, 0, 0, 0, {"skip": "Funding terlalu positif"}
    if ta_dir == "SHORT" and funding < -0.10:
        return None, 0, 0, 0, {"skip": "Funding terlalu negatif"}

    # ── 14. BB width (anti choppy) ────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    if bb_width < 0.007:
        return None, 0, 0, 0, {"skip": f"BB sempit ({bb_width:.4f})"}

    # ── 15. S/R check ─────────────────────────────────
    current_price = df_closed["close"].iloc[-1]
    sr_ok, sr_reason = check_sr_clear(symbol, current_price, ta_dir)
    if not sr_ok:
        return None, 0, 0, 0, {"skip": f"S/R: {sr_reason}"}

    # ── 16. 5m confirmation ───────────────────────────
    m5_ok, m5_info = get_5m_confirmation(symbol, ta_dir)
    if not m5_ok and score < min_score + 10:
        return None, 0, 0, 0, {"skip": f"5m contra + score pas-pasan"}

    # ── 17. Fee-aware SL/TP ───────────────────────────
    atr  = df_closed["atr"].iloc[-1]
    sl_p, tp1_p, tp2_p, sl_pct, tp1_pct, rr = calc_fee_aware_sltp(
        current_price, ta_dir, atr)

    if sl_pct > 3.0:
        return None, 0, 0, 0, {"skip": f"ATR besar (SL={sl_pct:.1f}%)"}
    if rr < 1.4:
        return None, 0, 0, 0, {"skip": f"R:R kurang ({rr:.2f})"}

    _last_candle[symbol] = prev_candle_time

    info = {
        "score":          f"{score:.1f}",
        "score_num":      score,
        "min_score":      min_score,
        "score_ctx":      score_ctx,
        "confluence":     confluence,
        "breakdown":      breakdown,
        "regime":         regime,
        "quality_gate":   quality_gate,
        "btc_15m":        _macro["btc_trend_15m"],
        "btc_1h":         _macro["btc_trend_1h"],
        "btc_4h":         _macro["btc_trend_4h"],
        "breadth":        f"{_macro['market_breadth']*100:.0f}%",
        "whale":          f"{whale_dir}({whale_ratio:.1f}x)",
        "funding":        funding,
        "bb_width":       round(bb_width, 4),
        "5m":             m5_info,
        "sl_pct":         f"{sl_pct:.2f}%",
        "tp1_pct":        f"{tp1_pct:.2f}%",
        "rr":             f"{rr:.2f}",
        "fng":            fng,
        "atr":            atr,
    }
    return ta_dir, sl_p, tp1_p, tp2_p, info

# ════════════════════════════════════════════════════
#  PARALLEL SCAN (v12)
# ════════════════════════════════════════════════════
def scan_symbol(symbol, allowed_dirs, quality_gate):
    """Worker untuk parallel scan. Market context dikirim dari main loop."""
    if symbol in open_positions: return None
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 230)
    if df is None or len(df) < 80: return None
    side, sl, tp1, tp2, info = should_enter(symbol, df, allowed_dirs, quality_gate)
    if side:
        return (symbol, side, sl, tp1, tp2, info)
    return None

def parallel_scan(symbols, allowed_dirs, quality_gate, max_workers=6):
    """Scan semua symbol secara paralel, return list candidates."""
    candidates = []
    skipped    = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scan_symbol, sym, allowed_dirs, quality_gate): sym
            for sym in symbols if sym not in open_positions
        }
        for future in as_completed(futures):
            try:
                result = future.result(timeout=15)
                if result:
                    candidates.append(result)
                else:
                    skipped += 1
            except Exception:
                skipped += 1
    return candidates, skipped

# ════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════
def open_trade(symbol, side, sl_price, tp1_price, tp2_price, info):
    global _consec_loss
    try:
        set_leverage(symbol)
        price = get_price(symbol)

        # Dynamic position sizing (v11)
        fraction = get_position_fraction(
            score      = info.get("score_num", 60),
            min_score  = info.get("min_score", BASE_MIN_SCORE),
            fng        = info.get("fng", 50),
            consec_loss= _consec_loss
        )
        qty = calc_qty(symbol, price, fraction=fraction)

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == "LONG" else SIDE_SELL,
            type=ORDER_TYPE_MARKET, quantity=qty)

        entry    = get_price(symbol)
        atr      = info.get("atr", (sl_price - entry) if side == "LONG" else (entry - sl_price))
        trail_sl = entry * (1 - ATR_TRAIL_MULT * atr / entry) if side == "LONG" \
                   else entry * (1 + ATR_TRAIL_MULT * atr / entry)

        open_positions[symbol] = {
            "side":        side,
            "entry":       entry,
            "qty":         qty,
            "qty_remain":  qty,
            "sl":          sl_price,
            "tp1":         tp1_price,
            "tp2":         tp2_price,
            "peak":        entry,
            "trail_sl":    trail_sl,
            "trailing_active": False,
            "tp1_hit":     False,
            "be_active":   False,
            "open_time":   time.time(),
            "atr":         atr,
            "fraction":    fraction,
        }

        active, session = is_active_session()
        conf = info.get("confluence", "?")
        print(f"\n  ✅ [{symbol}] {side} @{entry:.5f} qty={qty} (size={fraction*100:.0f}%)")
        print(f"     SL:{sl_price:.5f} (-{info.get('sl_pct','?')}) | "
              f"TP1:{tp1_price:.5f} (+{info.get('tp1_pct','?')}) | R:R={info.get('rr','?')}")
        print(f"     Score:{info.get('score','?')} (min:{info.get('min_score','?')}) | "
              f"Confluence:{conf}/{MIN_CONFLUENCE} | Session:{session}")
        print(f"     BTC:{info.get('btc_15m','?')}/{info.get('btc_1h','?')}/{info.get('btc_4h','?')} | "
              f"Breadth:{info.get('breadth','?')} | Regime:{info.get('regime','?')}")
        print(f"     Fee per round-trip: ~{entry * ROUND_TRIP_FEE * qty:.4f} USDT")
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal entry: {e}")

# ════════════════════════════════════════════════════
#  POSITION MANAGEMENT (v11)
# ════════════════════════════════════════════════════
def partial_close(symbol, reason="TP1"):
    pos = open_positions.get(symbol)
    if pos is None: return
    try:
        amt = get_exchange_amt(symbol)
        if amt is None or amt == 0:
            pos["tp1_hit"] = True; return

        close_qty = round_step(abs(amt) * 0.5, get_sym_info(symbol)["step"])
        close_qty = max(close_qty, get_sym_info(symbol)["minQty"])

        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=close_qty, reduceOnly=True)

        exit_price = get_price(symbol)
        side  = pos["side"]
        fee   = exit_price * TAKER_FEE * close_qty  # hanya exit fee (entry sudah dibayar)
        gross = (exit_price - pos["entry"]) * close_qty if side == "LONG" \
                else (pos["entry"] - exit_price) * close_qty
        net   = gross - fee - (pos["entry"] * TAKER_FEE * close_qty)  # entry + exit fee
        pct   = net / (pos["entry"] * close_qty) * 100

        print(f"  🎯 [{symbol}] PARTIAL {reason} @{exit_price:.5f}")
        print(f"     💛 Gross: {gross:+.4f}U | Fee: {fee:.4f}U | Net: {net:+.4f}U ({pct:+.2f}%)")

        pos["tp1_hit"]  = True
        pos["qty_remain"] = abs(amt) - close_qty
        pos["be_active"]  = True
        pos["sl"]         = pos["entry"] * (1 + ROUND_TRIP_FEE * 1.5) if side == "LONG" \
                            else pos["entry"] * (1 - ROUND_TRIP_FEE * 1.5)  # BE + fee buffer
        pos["trailing_active"] = True
        pos["peak"]     = exit_price
        # ATR-based trailing setelah TP1
        atr = pos.get("atr", exit_price * 0.005)
        pos["trail_sl"] = exit_price * (1 - ATR_TRAIL_MULT * 0.6 * atr / exit_price) if side == "LONG" \
                          else exit_price * (1 + ATR_TRAIL_MULT * 0.6 * atr / exit_price)

        print(f"     🔒 BE stop @{pos['sl']:.5f} (termasuk fee buffer)")
        trade_log.append({"symbol": symbol, "side": side, "pnl": round(net, 4), "reason": f"Partial {reason}"})
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal partial: {e}")
        pos["tp1_hit"] = True

def close_trade(symbol, reason=""):
    global _consec_loss, _in_cooldown
    try:
        amt = get_exchange_amt(symbol)
        if amt is None:
            print(f"  ⚠️  [{symbol}] Query gagal"); return False
        if amt == 0:
            if symbol in open_positions:
                pos   = open_positions[symbol]
                exit_ = get_price(symbol)
                if exit_ > 0:
                    qty_r = pos.get("qty_remain", pos["qty"])
                    gross = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                            else (pos["entry"] - exit_) * qty_r
                    fee   = exit_ * TAKER_FEE * qty_r + pos["entry"] * TAKER_FEE * qty_r
                    net   = gross - fee
                    print(f"  ⚠️  [{symbol}] Sudah tutup — Net P&L: {net:+.4f}U")
                    trade_log.append({"symbol": symbol, "side": pos["side"],
                                      "pnl": round(net, 4), "reason": "External close"})
                    _update_loss_streak(symbol, net)
                open_positions.pop(symbol, None)
            return True

        client.futures_create_order(
            symbol=symbol, side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET, quantity=abs(amt), reduceOnly=True)

        if symbol in open_positions:
            pos   = open_positions[symbol]
            exit_ = get_price(symbol)
            qty_r = pos.get("qty_remain", pos["qty"])
            gross = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                    else (pos["entry"] - exit_) * qty_r
            # Fee: entry fee + exit fee
            fee   = (pos["entry"] + exit_) * TAKER_FEE * qty_r
            net   = gross - fee
            pct   = net / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
            emoji = "🟢" if net >= 0 else "🔴"
            be_tag = " [BE]" if pos.get("be_active") else ""
            hold_min = (time.time() - pos.get("open_time", time.time())) / 60

            print(f"  💰 [{symbol}] CLOSED {reason}{be_tag} ({hold_min:.0f}min)")
            print(f"     {emoji} Gross:{gross:+.4f}U | Fee:{fee:.4f}U | Net:{net:+.4f}U ({pct:+.2f}%)")
            trade_log.append({"symbol": symbol, "side": pos["side"],
                              "pnl": round(net, 4), "reason": reason})
            _update_loss_streak(symbol, net)
        open_positions.pop(symbol, None)
        return True
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal close: {e}")
        return False

def _update_loss_streak(symbol, pnl):
    global _consec_loss, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        set_symbol_cooldown(symbol)
        if _consec_loss >= GLOBAL_COOLDOWN_LOSS:
            btc_bad     = _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < 0.30
            if btc_bad and breadth_bad:
                _in_cooldown = True
                print(f"  🧊 {GLOBAL_COOLDOWN_LOSS} loss + market buruk → Global Cooldown!")
            else:
                print(f"  ⚡ {_consec_loss} loss tapi market masih bisa → symbol cooldown aja")
                _consec_loss = 0
    else:
        _consec_loss = 0
        if _in_cooldown:
            _in_cooldown = False
            print(f"  ✅ Win! Global cooldown selesai.")

def manage_positions():
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_price(symbol)
        if price == 0: continue

        entry = pos["entry"]
        side  = pos["side"]
        atr   = pos.get("atr", price * 0.005)

        # ── Emergency exits ────────────────────────────
        if _macro["news"] == "strong_negative":
            close_trade(symbol, "🚨 Emergency news"); continue
        if side == "LONG" and _macro["btc_trend_1h"] == "BEAR" and _macro["btc_trend_4h"] == "BEAR":
            close_trade(symbol, "⚡ BTC 1H+4H BEAR"); continue
        if side == "SHORT" and _macro["btc_trend_1h"] == "BULL" and _macro["btc_trend_4h"] == "BULL":
            close_trade(symbol, "⚡ BTC 1H+4H BULL"); continue

        # ── Time-based forced exit (v11 BARU) ──────────
        hold_mins = (time.time() - pos.get("open_time", time.time())) / 60
        if hold_mins > MAX_HOLD_MINUTES and not pos["tp1_hit"]:
            close_trade(symbol, f"⏰ Max hold {MAX_HOLD_MINUTES}min"); continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            if not pos["tp1_hit"] and price >= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                # ATR-based trailing (lebih dinamis dari flat %)
                pos["trail_sl"] = price - ATR_TRAIL_MULT * atr
                print(f"  🔄 [{symbol}] ATR Trailing aktif @{price:.5f} ({profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = max(pos["trail_sl"], price - ATR_TRAIL_MULT * atr)

            if pos["tp1_hit"] and price >= pos["tp2"]:
                close_trade(symbol, "✨ TP2 hit"); continue

            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 ATR Trailing Stop"); continue

            if price <= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 SL"
                close_trade(symbol, reason); continue

            pnl_est = (price - entry) * pos.get("qty_remain", pos["qty"])
            tp_tag  = f"TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f"TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            be_tag  = " [BE]" if pos.get("be_active") else ""
            print(f"  📌 [{symbol}] LONG @{entry:.4f}→{price:.4f}{be_tag} | {pnl_est:+.3f}U{tsl} | {tp_tag}")

        else:  # SHORT
            profit_pct = (entry - price) / entry

            if not pos["tp1_hit"] and price <= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price + ATR_TRAIL_MULT * atr
                print(f"  🔄 [{symbol}] ATR Trailing aktif @{price:.5f} ({profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = min(pos["trail_sl"], price + ATR_TRAIL_MULT * atr)

            if pos["tp1_hit"] and price <= pos["tp2"]:
                close_trade(symbol, "✨ TP2 hit"); continue

            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 ATR Trailing Stop"); continue

            if price >= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 SL"
                close_trade(symbol, reason); continue

            pnl_est = (entry - price) * pos.get("qty_remain", pos["qty"])
            tp_tag  = f"TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f"TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            be_tag  = " [BE]" if pos.get("be_active") else ""
            print(f"  📌 [{symbol}] SHORT @{entry:.4f}→{price:.4f}{be_tag} | {pnl_est:+.3f}U{tsl} | {tp_tag}")

# ════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════
def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"] > 0)
    n     = len(trade_log)
    wr    = wins / n * 100 if n else 0
    # Estimasi break-even win rate berdasarkan R:R 1.6
    be_wr = 1 / (1 + RR_RATIO) * 100  # ~38.5% break-even
    cd_info = f" | 🧊 GlobalCD" if _in_cooldown else ""
    sym_cd  = f" | {len(_symbol_cooldown)} sym-cd" if _symbol_cooldown else ""
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% (BE:{be_wr:.0f}%) | "
          f"W:{wins} L:{n-wins} | Net P&L:{total:+.4f}U{cd_info}{sym_cd}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:40]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    print("🤖 Bot v12 — High-WR Directional Scalping")
    print(f"   Leverage    : {LEVERAGE}x | Order: ${ORDER_USDT} USDT")
    print(f"   Fee         : {TAKER_FEE*100:.3f}% per sisi ({ROUND_TRIP_FEE*100:.3f}% round-trip)")
    print(f"   SL/TP       : Fee-aware | R:R min {RR_RATIO}:1")
    print(f"   Score       : BASE={BASE_MIN_SCORE} + adaptive | Confluence min {MIN_CONFLUENCE}/7")
    print(f"   v12 KEY     : Market context dievaluasi per siklus — arah terlarang tidak masuk")
    print(f"                 BTC 4H+1H BULL → LONG only | BTC 4H+1H BEAR → SHORT only")
    print(f"                 Breadth <18% → SKIP semua | Counter-trend threshold +22 (was +18)")
    print(f"   Trailing    : ATR-based (aktif setelah +{TRAIL_TRIGGER*100:.1f}%)")
    print(f"   Max hold    : {MAX_HOLD_MINUTES} menit (paksa exit)")
    print(f"   Symbols     : {len(SYMBOLS)} coin (akan difilter yang tidak tersedia)\n")

    print("  ⏳ Validasi symbols & setup...")
    symbols = validate_symbols()
    for s in symbols[:20]: get_sym_info(s)  # preload sym info untuk 20 pertama
    refresh_macro()

    active, session = is_active_session()
    print(f"  ✅ {len(symbols)} symbols | F&G:{_macro['fng']}({_macro['fng_label']}) | "
          f"BTC:{_macro['btc_trend_15m']}/{_macro['btc_trend_1h']}/{_macro['btc_trend_4h']}")
    print(f"  📅 Session: {session} | Breadth:{_macro['market_breadth']*100:.0f}% | "
          f"News:{_macro['news']}\n")

    cycle = 0
    while True:
        cycle += 1
        refresh_macro()
        if _in_cooldown: is_cooldown_active()

        manage_positions()

        active, session = is_active_session()
        cd_info = " 🧊 GlobalCD" if _in_cooldown else ""
        sym_cd  = f" | {len(_symbol_cooldown)}sym-cd" if _symbol_cooldown else ""
        print(f"\n{'='*76}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | "
              f"USDT.D:{_macro['usdt_d']}% | News:{_macro['news']}{cd_info}")
        print(f"  📈 BTC: 15m={_macro['btc_trend_15m']} | 1H={_macro['btc_trend_1h']} | "
              f"4H={_macro['btc_trend_4h']}")
        print(f"  🌍 Breadth:{_macro['market_breadth']*100:.0f}% | MCap24h:{_macro['global_mcap_chg']:+.1f}% | "
              f"Session:{session}{sym_cd}")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}/{MAX_POSITIONS}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*76}")

        if len(open_positions) < MAX_POSITIONS and not _in_cooldown and \
           _macro["news"] != "strong_negative":

            # ── v12: Evaluasi market context SEKALI per siklus ──
            allowed_dirs, quality_gate, ctx_reason = get_market_context()

            if not allowed_dirs:
                # SKIP = kondisi terlalu buruk untuk scan
                print(f"  🚫 SKIP SCAN — {ctx_reason}")
            else:
                gate_icon = "⭐" if quality_gate == "PREMIUM" else "✓"
                print(f"  🔍 Scanning {len(symbols)} symbols | {gate_icon} {quality_gate} | Dir:{'/'.join(sorted(allowed_dirs))} | {ctx_reason}")
                candidates, skipped = parallel_scan(symbols, allowed_dirs, quality_gate)

                if candidates:
                    candidates.sort(key=lambda x: x[5].get("score_num", 0), reverse=True)
                    print(f"\n  🎯 {len(candidates)} setup valid | {skipped} di-skip")
                    for sym, side, sl, tp1, tp2, info in candidates[:3]:
                        print(f"     ⭐ {sym} {side} | Score:{info.get('score','?')} | "
                              f"Confluence:{info.get('confluence','?')}/{MIN_CONFLUENCE} | "
                              f"R:R:{info.get('rr','?')} | Gate:{info.get('quality_gate','?')}")
                    for sym, side, sl, tp1, tp2, info in candidates:
                        if len(open_positions) >= MAX_POSITIONS: break
                        open_trade(sym, side, sl, tp1, tp2, info)
                else:
                    print(f"  ⏳ {skipped} di-scan, belum ada setup valid saat ini")
        else:
            if _in_cooldown:
                print(f"  🧊 Global Cooldown — {cooldown_reason()}")
            else:
                print(f"  ⏸️  Posisi penuh ({len(open_positions)}/{MAX_POSITIONS})")

        print_summary()
        print(f"\n  ⏱️  Next scan dalam {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
