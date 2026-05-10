"""
Bot Trading v13 — Precision High-WR Scalping
=============================================
CHANGELOG dari v12:

═══ 1. VOLATILITY-TIERED SL CAP (KRITIS) ════════════════════════
  v12: SL flat max 3.0% untuk semua coin — IOSTUSDT bisa kena -3.5U
  v13: SL cap berdasarkan market cap tier coin:
       LARGE  (BTC/ETH/BNB/SOL/XRP): max 2.2% SL
       MID    (top 30 alts): max 1.8% SL
       SMALL  (micro/meme): max 1.2% SL — KETAT
       ATR_SL_MULT juga diturunkan untuk small cap

═══ 2. SESSION-AWARE ENTRY GATE ══════════════════════════════════
  v12: OFF session hanya +10 threshold penalty — masih bisa masuk
  v13: OFF session + mixed BTC = hard gate:
       - Score harus > 72 (was 55+10=65)
       - Size max 60% (was normal)
       - Small/micro cap DIBLOKIR sepenuhnya saat OFF session
       - Hanya LARGE+MID yang boleh trade off-session

═══ 3. ANTI-WHIPSAW MOMENTUM FILTER ══════════════════════════════
  v12: Entry bisa terjadi walau candle sebelumnya "noise"
  v13: Pre-entry momentum consistency check:
       LONG: min 2 dari 3 candle sebelum = green + close > open 50%
       SHORT: min 2 dari 3 candle sebelum = red
       PLUS: body/range ratio > 0.35 (bukan doji/spinning top)

═══ 4. WEIGHTED LOSS STREAK ══════════════════════════════════════
  v12: consec_loss hanya hitung jumlah, -3.5U sama dengan -0.1U
  v13: Loss magnitude weighting:
       Loss > 2x avg_win → streak weight +2 (bukan +1)
       Loss > 0.5U = "significant loss" → symbol cooldown 10 menit

═══ 5. SMARTER TP1 TRAILING ══════════════════════════════════════
  v12: ATR trailing flat setelah TP1
  v13: Progressive trail — makin dekat TP2, trail makin ketat:
       Phase 1 (TP1 → 50% ke TP2): trail 0.8x ATR
       Phase 2 (50% → 75% ke TP2): trail 0.5x ATR
       Phase 3 (>75% ke TP2): trail 0.3x ATR (lock profit aggresif)

═══ 6. CORRELATION FILTER ════════════════════════════════════════
  v13 BARU: Jika sudah ada posisi di coin yang berkorelasi tinggi
  (misal: 1INCH, ZRX, SUSHI = semua DeFi token), tidak ambil posisi
  ke-2 di sektor yang sama. Hindari concentrated exposure.

═══ 7. CANDLE QUALITY SCORE ══════════════════════════════════════
  v13 BARU: Tambahan filter kualitas candle sebelum entry:
       - Candle entry harus punya body/range > 0.40 (bukan doji)
       - Upper/lower wick tidak boleh > 2x body (rejection candle)
       - Spread harus konsisten (tidak ada gap besar)

═══ 8. ADAPTIVE PROFIT TARGET ════════════════════════════════════
  v13 BARU: TP1 disesuaikan berdasarkan session:
       OVERLAP session: TP1 lebih agresif (RR 2.0x)
       OFF session: TP1 lebih konservatif (RR 1.4x, exit cepat)
       NORMAL session: RR 1.6x (default)

═══ 9. MEME COIN FILTER ══════════════════════════════════════════
  v13 BARU: Blacklist coin yang terlalu volatile / manipulasi tinggi
  selama off-session atau breadth < 45%.

═══ DISCLAIMER ══════════════════════════════════════════════════
  Trading crypto futures dengan leverage mengandung risiko kehilangan
  seluruh modal. Bot ini bukan jaminan profit. Selalu gunakan testnet
  dulu minimal 2-4 minggu sebelum live trading.
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
#  COIN CLASSIFICATION (v13 BARU — KRITIS untuk SL sizing)
# ════════════════════════════════════════════════════
LARGE_CAP = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "TRXUSDT", "DOTUSDT",
}

MID_CAP = {
    "LINKUSDT", "MATICUSDT", "LTCUSDT", "ATOMUSDT", "UNIUSDT",
    "ETCUSDT", "XLMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "INJUSDT", "SUIUSDT", "TIAUSDT", "AAVEUSDT",
    "RUNEUSDT", "FILUSDT", "LDOUSDT", "MKRUSDT", "SNXUSDT",
    "GMXUSDT", "DYDXUSDT", "PENDLEUSDT", "FETUSDT", "RNDRUSDT",
    "THETAUSDT", "VETUSDT", "SANDUSDT", "MANAUSDT", "AXSUSDT",
}

# Semua selain LARGE+MID = SMALL_CAP → SL lebih ketat
# Coin ini DIBLOKIR saat OFF session atau breadth rendah
HIGH_VOLATILITY_BLACKLIST = {
    "WUSDT", "BOMEUSDT", "PIXELUSDT", "RONINUSDT",
    "MEMEUSDT", "PORTALUSDT", "ACEUSDT", "XAIUSDT",
    "AIUSDT", "DOGUSDT", "1000PEPEUSDT", "FLOKIUSDT",
    "BONKUSDT", "1000SHIBUSDT",
}

# Sektor correlation groups — hindari double exposure
SECTOR_GROUPS = {
    "defi_dex":    {"UNIUSDT", "SUSHIUSDT", "1INCHUSDT", "BALUSDT", "CRVUSDT", "DYDXUSDT"},
    "defi_lend":   {"AAVEUSDT", "COMPUSDT", "MKRUSDT", "LQTYUSDT", "RDNTUSDT"},
    "defi_perp":   {"GMXUSDT", "PERPUSDT", "DYDXUSDT", "UMAUSDT"},
    "layer2":      {"MATICUSDT", "ARBUSDT", "OPUSDT", "STRKUSDT", "ZRXUSDT"},
    "ai_data":     {"FETUSDT", "AGIXUSDT", "OCEANUSDT", "RNDRUSDT"},
    "meme":        {"DOGEUSDT", "1000SHIBUSDT", "1000PEPEUSDT", "FLOKIUSDT", "BONKUSDT", "WIFUSDT"},
    "gamefi":      {"SANDUSDT", "MANAUSDT", "AXSUSDT", "GALAUSDT", "ENJUSDT", "IMXUSDT"},
    "storage":     {"FILUSDT", "STORJUSDT", "CKBUSDT"},
    "oracle":      {"LINKUSDT", "BANDUSDT"},
    "infra":       {"DOTUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT"},
}

def get_coin_tier(symbol):
    if symbol in LARGE_CAP: return "LARGE"
    if symbol in MID_CAP:   return "MID"
    return "SMALL"

def get_tier_sl_cap(tier, session_active):
    """v13: SL cap berdasarkan tier. Lebih ketat saat off-session."""
    caps = {
        "LARGE": 2.2 if session_active else 1.8,
        "MID":   1.8 if session_active else 1.4,
        "SMALL": 1.2 if session_active else 0.9,
    }
    return caps.get(tier, 1.2)

def get_sector(symbol):
    for sector, coins in SECTOR_GROUPS.items():
        if symbol in coins: return sector
    return None

def is_sector_exposed(symbol):
    """True jika sudah ada posisi terbuka di sektor yang sama."""
    sector = get_sector(symbol)
    if sector is None: return False
    for open_sym in open_positions:
        if get_sector(open_sym) == sector:
            return True, sector
    return False, None

# ════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════
LEVERAGE              = 10
ORDER_USDT            = 10
TAKER_FEE             = 0.0004
ROUND_TRIP_FEE        = TAKER_FEE * 2

# SL/TP base (tier-adjusted per coin)
ATR_SL_MULT_LARGE     = 1.8
ATR_SL_MULT_MID       = 1.5
ATR_SL_MULT_SMALL     = 1.1   # jauh lebih ketat untuk small cap
RR_RATIO              = 1.6   # default

# RR per session (v13)
RR_OVERLAP            = 2.0   # session overlap → lebih agresif karena lebih likuid
RR_ACTIVE             = 1.6   # session normal
RR_OFF                = 1.35  # off session → exit lebih cepat

# Progressive trailing phases (v13)
TRAIL_TRIGGER         = 0.004
ATR_TRAIL_PHASE1      = 0.80  # TP1 → 50% ke TP2
ATR_TRAIL_PHASE2      = 0.50  # 50% → 75% ke TP2
ATR_TRAIL_PHASE3      = 0.30  # >75% ke TP2 (lock aggresif)

# Position & risk
MAX_POSITIONS         = 3
MAX_HOLD_MINUTES      = 90    # dikurangi dari 120 (v13) — exit lebih cepat
MAX_HOLD_OFF_SESSION  = 60    # maksimum hold saat off-session
MAX_CONSEC_LOSS       = 2
SCAN_INTERVAL         = 40

# Score thresholds
BASE_MIN_SCORE        = 57    # naik sedikit dari 55
BASE_MIN_SCORE_OFF    = 72    # hard gate saat OFF session (v13 BARU)
MIN_CONFLUENCE        = 3
SCORE_BONUS_BTC_ALIGN = 8
OFF_SESSION_PENALTY   = 15    # naik dari 10

# Macro filters
MIN_FNG_ANY           = 15
MAX_FNG_LONG          = 88
FNG_FEAR_ZONE         = 38
FNG_GREED_ZONE        = 78
MIN_MARKET_BREADTH    = 0.35  # naik dari 0.32 (lebih ketat)
SR_BUFFER             = 0.004
MIN_VOLUME_SPIKE      = 1.25
USDT_RISK_OFF_DELTA   = 0.05

# Loss management (v13 weighted)
SIGNIFICANT_LOSS_USDT = 0.6   # loss lebih dari ini = significant
SYMBOL_COOLDOWN_SECS  = 360
SYMBOL_COOLDOWN_BIG   = 600   # cooldown lebih panjang setelah loss besar (v13)
GLOBAL_COOLDOWN_LOSS  = 4
COOLDOWN_BTC_BAD      = {"BEAR"}
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL", "SIDEWAYS"}
COOLDOWN_BREADTH_MIN  = 0.38

# Candle quality thresholds (v13 BARU)
MIN_BODY_RANGE_RATIO  = 0.38  # body/range — filter doji
MAX_WICK_BODY_RATIO   = 2.2   # max (upper_wick + lower_wick) / body

# Session filter (UTC)
ACTIVE_SESSIONS = {
    "ASIA_OPEN":  (0, 4),
    "EU_OPEN":    (7, 11),
    "NY_OPEN":    (13, 17),
    "OVERLAP":    (13, 15),
}

# ══ COMPOSITE SCORE WEIGHTS ════════════════════════
SCORE_WEIGHTS = {
    "macd_cross":   22,
    "ema_stack":    18,
    "rsi":          15,
    "volume":       15,
    "ob_imbalance": 12,
    "stoch_cross":  10,
    "cum_delta":     8,
}

# ══ TOP 100 BINANCE FUTURES COINS ══════════════════
SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","TRXUSDT","DOTUSDT",
    "LINKUSDT","MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT",
    "ETCUSDT","XLMUSDT","NEARUSDT","APTUSDT","ARBUSDT",
    "OPUSDT","INJUSDT","SUIUSDT","TIAUSDT","AAVEUSDT",
    "RUNEUSDT","FILUSDT","LDOUSDT","MKRUSDT","SNXUSDT",
    "1000PEPEUSDT","WIFUSDT","JUPUSDT","SEIUSDT","PYTHUSDT",
    "WLDUSDT","STRKUSDT","ALTUSDT","DYMUSDT",
    "RONINUSDT","PIXELUSDT","PORTALUSDT","ACEUSDT","XAIUSDT",
    "MANTAUSDT","ZETAUSDT","AIUSDT","WUSDT","BOMEUSDT",
    "SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","ENJUSDT",
    "CHZUSDT","FLOWUSDT","IMXUSDT","LOOMUSDT","SKLUSDT",
    "CELOUSDT","ZRXUSDT","BANDUSDT","STORJUSDT","CRVUSDT",
    "COMPUSDT","YFIUSDT","SUSHIUSDT","1INCHUSDT","BALUSDT",
    "GMXUSDT","DYDXUSDT","PERPUSDT","BLURUSDT","MAGICUSDT",
    "RDNTUSDT","PENDLEUSDT","UMAUSDT","LITUSDT","LQTYUSDT",
    "FLOKIUSDT","1000SHIBUSDT","BONKUSDT","MEMEUSDT","DOGUSDT",
    "FETUSDT","AGIXUSDT","OCEANUSDT","RNDRUSDT","THETAUSDT",
    "VETUSDT","ZILUSDT","IOSTUSDT","ONEUSDT","ANKRUSDT",
    "CELRUSDT","REEFUSDT","SFPUSDT","CKBUSDT","ACAUSDT",
    "ROSEUSDT","WOOUSDT","APEUSDT","GALUSDT","HOOKUSDT",
    "HIGHUSDT","MINAUSDT","CFXUSDT","STXUSDT",
    "KASUSDT","ORDIUSDT","SATSUSDT","TAOUSDT","JTOUSDT",
]
SYMBOLS = list(dict.fromkeys(SYMBOLS))

# ════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════
open_positions    = {}
trade_log         = []
_last_candle      = {}
_consec_loss      = 0
_loss_weight      = 0.0   # v13: weighted loss counter
_in_cooldown      = False
_symbol_cooldown  = {}
_scan_lock        = threading.Lock()

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
    raw  = (ORDER_USDT * LEVERAGE * fraction) / price
    return max(round_step(raw, info["step"]), info["minQty"])

def get_position_fraction(score, min_score, fng, loss_weight, tier, session_active):
    """
    v13: Dynamic sizing dengan tier + loss weight awareness.
    Small cap off-session punya size minimum.
    """
    fraction = 1.0

    # Tier-based max fraction
    tier_max = {"LARGE": 1.0, "MID": 0.90, "SMALL": 0.75}.get(tier, 0.75)
    fraction = min(fraction, tier_max)

    # Score margin
    score_margin = score - min_score
    if score_margin < 8:    fraction *= 0.68
    elif score_margin < 15: fraction *= 0.82

    # F&G extreme
    if fng < FNG_FEAR_ZONE or fng > FNG_GREED_ZONE:
        fraction *= 0.80

    # Weighted loss streak (v13) — lebih agresif dari v12
    if loss_weight >= 4.0:   fraction *= 0.50   # heavy loss streak
    elif loss_weight >= 2.0: fraction *= 0.65
    elif loss_weight >= 1.0: fraction *= 0.80

    # Off session penalty
    if not session_active:
        fraction *= 0.70

    return max(fraction, 0.40)  # absolute minimum 40%

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
    if 13 <= utc_hour < 15: return True, "OVERLAP"
    for name, (start, end) in ACTIVE_SESSIONS.items():
        if name == "OVERLAP": continue
        if start <= utc_hour < end: return True, name
    return False, "OFF"

def get_avg_win():
    """Estimasi avg win dari trade log (untuk weighted loss calculation)."""
    wins = [t["pnl"] for t in trade_log if t["pnl"] > 0]
    return sum(wins) / len(wins) if wins else 0.5

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
#  CANDLE QUALITY CHECK (v13 BARU)
# ════════════════════════════════════════════════════
def check_candle_quality(df, direction):
    """
    v13: Filter candle jelek sebelum entry.
    Tolak: doji, spinning top, rejection candles (long wick).
    """
    last = df.iloc[-1]
    body  = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]
    if range_ == 0: return False, "zero range candle"

    body_ratio = body / range_
    if body_ratio < MIN_BODY_RANGE_RATIO:
        return False, f"Doji/spinning ({body_ratio:.2f} body ratio)"

    # Check wick dominance
    if direction == "LONG":
        upper_wick = last["high"] - max(last["close"], last["open"])
        if body > 0 and upper_wick / body > MAX_WICK_BODY_RATIO:
            return False, f"Upper wick terlalu panjang ({upper_wick/body:.1f}x body)"
    else:
        lower_wick = min(last["close"], last["open"]) - last["low"]
        if body > 0 and lower_wick / body > MAX_WICK_BODY_RATIO:
            return False, f"Lower wick terlalu panjang ({lower_wick/body:.1f}x body)"

    # Momentum consistency (v13) — 3 candle sebelumnya harus konsisten
    recent = df.iloc[-4:-1]
    if direction == "LONG":
        green_count = sum(1 for _, r in recent.iterrows() if r["close"] > r["open"])
        if green_count < 2:
            return False, f"Momentum lemah ({green_count}/3 green candle)"
    else:
        red_count = sum(1 for _, r in recent.iterrows() if r["close"] < r["open"])
        if red_count < 2:
            return False, f"Momentum lemah ({red_count}/3 red candle)"

    return True, "OK"

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

def get_market_context():
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]
    breadth = _macro["market_breadth"]

    bull_tf = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
    bear_tf = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)

    if breadth < 0.18:
        return set(), "SKIP", f"Breadth ekstrem rendah {breadth*100:.0f}% (<18%) — semua skip"

    if t4h in BULL_TRENDS and t1h in BULL_TRENDS:
        if breadth >= MIN_MARKET_BREADTH:
            return {"LONG"}, "PREMIUM", f"BTC 4H+1H BULL ({t4h}/{t1h}) — LONG only"
        else:
            return {"LONG"}, "NORMAL", f"BTC 4H+1H BULL tapi breadth {breadth*100:.0f}%"

    if t4h in BEAR_TRENDS and t1h in BEAR_TRENDS:
        if breadth <= 0.55:
            return {"SHORT"}, "PREMIUM", f"BTC 4H+1H BEAR ({t4h}/{t1h}) — SHORT only"
        else:
            return {"SHORT"}, "NORMAL", f"BTC 4H+1H BEAR tapi breadth tinggi {breadth*100:.0f}%"

    if bull_tf >= 2:
        return {"LONG", "SHORT"}, "NORMAL", f"BTC mixed bullish {bull_tf}/3 TF"
    if bear_tf >= 2:
        return {"LONG", "SHORT"}, "NORMAL", f"BTC mixed bearish {bear_tf}/3 TF"

    if breadth < MIN_MARKET_BREADTH:
        return {"SHORT"}, "NORMAL", f"BTC sideways + breadth rendah {breadth*100:.0f}%"
    if breadth > 0.68:
        return {"LONG"}, "NORMAL", f"BTC sideways + breadth tinggi {breadth*100:.0f}%"

    return {"LONG", "SHORT"}, "NORMAL", f"BTC sideways, breadth {breadth*100:.0f}% netral"

def get_adaptive_min_score(direction, quality_gate="NORMAL", session_active=True, session_name="OFF"):
    min_score = BASE_MIN_SCORE
    bonuses   = []
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS)
    else:
        btc_align = sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS)

    if btc_align >= 3:
        bonus = SCORE_BONUS_BTC_ALIGN + (3 if quality_gate == "PREMIUM" else 0)
        min_score -= bonus
        bonuses.append(f"BTC3TF-{bonus}")
    elif btc_align >= 2:
        bonus = SCORE_BONUS_BTC_ALIGN // 2
        min_score -= bonus
        bonuses.append(f"BTC2TF-{bonus}")
    else:
        min_score += 8
        bonuses.append("BTC_noalign+8")

    # v13: Session dengan hard gate
    if session_name == "OVERLAP":
        min_score -= 5; bonuses.append("OVERLAP-5")
    elif session_active:
        min_score -= 2; bonuses.append(f"{session_name}-2")
    else:
        # OFF session: gunakan hard minimum (lebih tinggi dari threshold)
        min_score = max(min_score + OFF_SESSION_PENALTY, BASE_MIN_SCORE_OFF)
        bonuses.append(f"OFF+{OFF_SESSION_PENALTY}→min{BASE_MIN_SCORE_OFF}")

    fng = _macro["fng"]
    if direction == "LONG" and fng < FNG_FEAR_ZONE:
        min_score += 6; bonuses.append("FNG_FEAR+6")
    elif direction == "SHORT" and fng < FNG_FEAR_ZONE:
        min_score -= 4; bonuses.append("FNG_FEAR_SHORT-4")
    elif direction == "LONG" and fng > FNG_GREED_ZONE:
        min_score -= 3; bonuses.append("FNG_GREED-3")

    breadth = _macro["market_breadth"]
    if breadth < 0.30 and direction == "LONG":
        min_score += 10; bonuses.append(f"LOW_BREADTH+10")
    elif breadth > 0.70 and direction == "SHORT":
        min_score += 10; bonuses.append(f"HIGH_BREADTH+10")

    return max(min_score, 42), bonuses

# ════════════════════════════════════════════════════
#  FEE-AWARE SL/TP CALCULATOR (v13 — tier-aware)
# ════════════════════════════════════════════════════
def calc_fee_aware_sltp(price, direction, atr, tier="MID", session_active=True, session_name="OFF"):
    """
    v13: SL dan TP disesuaikan dengan:
    - Tier coin (LARGE/MID/SMALL) → SL cap berbeda
    - Session → RR ratio berbeda
    - Fee awareness tetap dipertahankan
    """
    fee_dist = price * ROUND_TRIP_FEE

    # ATR multiplier per tier
    atr_mult = {
        "LARGE": ATR_SL_MULT_LARGE,
        "MID":   ATR_SL_MULT_MID,
        "SMALL": ATR_SL_MULT_SMALL,
    }.get(tier, ATR_SL_MULT_MID)

    # SL distance
    sl_dist_raw = atr_mult * atr
    sl_dist     = max(sl_dist_raw, fee_dist * 2.5)

    # Apply tier SL cap (CRITICAL untuk small cap)
    sl_cap_pct = get_tier_sl_cap(tier, session_active)
    sl_dist_max = price * sl_cap_pct / 100
    sl_dist     = min(sl_dist, sl_dist_max)

    # RR ratio per session
    if session_name == "OVERLAP":  rr = RR_OVERLAP
    elif session_active:           rr = RR_ACTIVE
    else:                          rr = RR_OFF

    tp1_dist = max(sl_dist * rr, fee_dist * 3.5)
    tp2_dist = tp1_dist * 2.2

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
    rr_act  = tp1_dist / sl_dist

    return sl_p, tp1_p, tp2_p, sl_pct, tp1_pct, rr_act

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
    last = df.iloc[-1]
    prev = df.iloc[-2]
    W    = SCORE_WEIGHTS
    breakdown = {}
    long_pts = short_pts = 0.0
    long_signals = short_signals = 0

    # ── 1. MACD Cross (22) ───────────────────────────
    hist_now  = last["macd_hist"]
    hist_prev = prev["macd_hist"]
    macd_cross_long  = hist_now > 0 and hist_prev <= 0
    macd_cross_short = hist_now < 0 and hist_prev >= 0
    macd_trend_long  = hist_now > 0 and hist_now > hist_prev
    macd_trend_short = hist_now < 0 and hist_now < hist_prev

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

    # ── 2. EMA Stack (18) ────────────────────────────
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

    # ── 3. RSI (15) ──────────────────────────────────
    rsi      = last["rsi"]
    rsi_prev = prev["rsi"]
    if rsi < 38 and rsi > rsi_prev:
        pts = W["rsi"] if rsi < 30 else W["rsi"] * 0.65
        long_pts += pts; long_signals += 1
        breakdown["rsi"] = f"+{pts:.0f}L({rsi:.0f}↑)"
    elif rsi > 62 and rsi < rsi_prev:
        pts = W["rsi"] if rsi > 70 else W["rsi"] * 0.65
        short_pts += pts; short_signals += 1
        breakdown["rsi"] = f"+{pts:.0f}S({rsi:.0f}↓)"
    else:
        breakdown["rsi"] = f"0({rsi:.0f})"

    # ── 4. Volume (15) ───────────────────────────────
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

    # ── 5. Order Book Imbalance (12) ─────────────────
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

    # ── 6. Stochastic Cross (10) ─────────────────────
    k, d    = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    stoch_cross_long  = k > d and pk <= pd_ and k < 35
    stoch_cross_short = k < d and pk >= pd_ and k > 65
    if stoch_cross_long:
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

    # ── 7. Cumulative Delta (8) ───────────────────────
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

    max_possible = sum(W.values())
    long_pct  = min(long_pts  / max_possible * 100, 100)
    short_pct = min(short_pts / max_possible * 100, 100)

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
#  COOLDOWN MANAGEMENT
# ════════════════════════════════════════════════════
def is_symbol_in_cooldown(symbol):
    if symbol not in _symbol_cooldown: return False
    elapsed = time.time() - _symbol_cooldown[symbol]["ts"]
    duration = _symbol_cooldown[symbol].get("duration", SYMBOL_COOLDOWN_SECS)
    if elapsed > duration:
        del _symbol_cooldown[symbol]; return False
    return True

def get_symbol_cooldown_remaining(symbol):
    if symbol not in _symbol_cooldown: return 0
    duration = _symbol_cooldown[symbol].get("duration", SYMBOL_COOLDOWN_SECS)
    return max(0, duration - (time.time() - _symbol_cooldown[symbol]["ts"]))

def set_symbol_cooldown(symbol, loss_usdt=0.0):
    """v13: Cooldown lebih lama untuk loss signifikan."""
    duration = SYMBOL_COOLDOWN_BIG if abs(loss_usdt) >= SIGNIFICANT_LOSS_USDT else SYMBOL_COOLDOWN_SECS
    _symbol_cooldown[symbol] = {"ts": time.time(), "duration": duration}
    print(f"  🧊 [{symbol}] Cooldown {duration}s (loss={loss_usdt:+.4f}U)")

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
#  MASTER ENTRY FILTER (v13)
# ════════════════════════════════════════════════════
def should_enter(symbol, df, allowed_dirs=None, quality_gate="NORMAL"):
    if allowed_dirs is None:
        allowed_dirs = {"LONG", "SHORT"}

    active, session = is_active_session()
    tier = get_coin_tier(symbol)

    # ── 0. Symbol cooldown ────────────────────────────
    if is_symbol_in_cooldown(symbol):
        remaining = get_symbol_cooldown_remaining(symbol)
        return None, 0, 0, 0, {"skip": f"⏳ Cooldown {remaining:.0f}s"}

    # ── 0b. Blacklist check (v13) ─────────────────────
    if symbol in HIGH_VOLATILITY_BLACKLIST:
        breadth = _macro["market_breadth"]
        if not active or breadth < 0.45:
            return None, 0, 0, 0, {"skip": f"High-vol blacklist (off/low breadth)"}

    # ── 0c. Small cap off-session block (v13) ─────────
    if tier == "SMALL" and not active:
        return None, 0, 0, 0, {"skip": f"SMALL cap diblokir saat OFF session"}

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

    # ── 5. Direction vs market context ────────────────
    if ta_dir not in allowed_dirs:
        return None, 0, 0, 0, {"skip": f"Dir {ta_dir} dilarang (macro:{'/'.join(allowed_dirs)})"}

    # ── 6. Confluence check ───────────────────────────
    if confluence < MIN_CONFLUENCE:
        return None, 0, 0, 0, {"skip": f"Confluence rendah ({confluence}/{MIN_CONFLUENCE})"}

    # ── 7. Adaptive min score (v13: session-aware) ────
    min_score, score_ctx = get_adaptive_min_score(ta_dir, quality_gate, active, session)
    if score < min_score:
        return None, 0, 0, 0, {"skip": f"Score {score:.1f} < min {min_score} ({session})"}

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
    if ta_dir == "LONG" and regime == "BEAR" and score < min_score + 22:
        return None, 0, 0, 0, {"skip": "Counter-trend LONG di BEAR"}
    if ta_dir == "SHORT" and regime == "BULL" and score < min_score + 22:
        return None, 0, 0, 0, {"skip": "Counter-trend SHORT di BULL"}

    # ── 11. Candle quality check (v13 BARU) ───────────
    cq_ok, cq_reason = check_candle_quality(df_closed, ta_dir)
    if not cq_ok:
        if score < min_score + 8:  # toleransi hanya kalau score sangat tinggi
            return None, 0, 0, 0, {"skip": f"CandleQ: {cq_reason}"}

    # ── 12. Sector correlation check (v13 BARU) ───────
    sector_exposed, sector_name = is_sector_exposed(symbol)
    if sector_exposed:
        return None, 0, 0, 0, {"skip": f"Sektor {sector_name} sudah ada posisi"}

    # ── 13. Volume check ──────────────────────────────
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

    # ── 14. Whale filter ──────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    if ta_dir == "LONG" and whale_dir == "sell_whale":
        return None, 0, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, 0, {"skip": "Whale buy aktif"}

    # ── 15. Funding extreme ───────────────────────────
    if ta_dir == "LONG" and funding > 0.10:
        return None, 0, 0, 0, {"skip": "Funding terlalu positif"}
    if ta_dir == "SHORT" and funding < -0.10:
        return None, 0, 0, 0, {"skip": "Funding terlalu negatif"}

    # ── 16. BB width (anti choppy) ────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    if bb_width < 0.007:
        return None, 0, 0, 0, {"skip": f"BB sempit ({bb_width:.4f})"}

    # ── 17. S/R check ─────────────────────────────────
    current_price = df_closed["close"].iloc[-1]
    sr_ok, sr_reason = check_sr_clear(symbol, current_price, ta_dir)
    if not sr_ok:
        return None, 0, 0, 0, {"skip": f"S/R: {sr_reason}"}

    # ── 18. 5m confirmation ───────────────────────────
    m5_ok, m5_info = get_5m_confirmation(symbol, ta_dir)
    if not m5_ok and score < min_score + 10:
        return None, 0, 0, 0, {"skip": f"5m contra + score pas-pasan"}

    # ── 19. Fee-aware SL/TP (tier + session aware) ────
    atr  = df_closed["atr"].iloc[-1]
    sl_p, tp1_p, tp2_p, sl_pct, tp1_pct, rr = calc_fee_aware_sltp(
        current_price, ta_dir, atr, tier, active, session)

    # Strict SL cap check (hard limit per tier)
    sl_hard_cap = get_tier_sl_cap(tier, active)
    if sl_pct > sl_hard_cap:
        return None, 0, 0, 0, {"skip": f"SL {sl_pct:.1f}% > cap {sl_hard_cap}% ({tier})"}
    if rr < 1.3:
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
        "tier":           tier,
        "session":        session,
        "session_active": active,
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
        "candle_ok":      cq_ok,
    }
    return ta_dir, sl_p, tp1_p, tp2_p, info

# ════════════════════════════════════════════════════
#  PARALLEL SCAN
# ════════════════════════════════════════════════════
def scan_symbol(symbol, allowed_dirs, quality_gate):
    if symbol in open_positions: return None
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 230)
    if df is None or len(df) < 80: return None
    side, sl, tp1, tp2, info = should_enter(symbol, df, allowed_dirs, quality_gate)
    if side:
        return (symbol, side, sl, tp1, tp2, info)
    return None

def parallel_scan(symbols, allowed_dirs, quality_gate, max_workers=6):
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
    global _consec_loss, _loss_weight
    try:
        set_leverage(symbol)
        price = get_price(symbol)
        tier  = info.get("tier", "MID")

        fraction = get_position_fraction(
            score          = info.get("score_num", 60),
            min_score      = info.get("min_score", BASE_MIN_SCORE),
            fng            = info.get("fng", 50),
            loss_weight    = _loss_weight,
            tier           = tier,
            session_active = info.get("session_active", True),
        )
        qty = calc_qty(symbol, price, fraction=fraction)

        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == "LONG" else SIDE_SELL,
            type=ORDER_TYPE_MARKET, quantity=qty)

        entry = get_price(symbol)
        atr   = info.get("atr", abs(sl_price - entry))
        tp2   = tp2_price
        tp1   = tp1_price

        # Initial trailing SL
        trail_sl = entry * (1 - ATR_TRAIL_PHASE1 * atr / entry) if side == "LONG" \
                   else entry * (1 + ATR_TRAIL_PHASE1 * atr / entry)

        open_positions[symbol] = {
            "side":             side,
            "entry":            entry,
            "qty":              qty,
            "qty_remain":       qty,
            "sl":               sl_price,
            "tp1":              tp1_price,
            "tp2":              tp2_price,
            "peak":             entry,
            "trail_sl":         trail_sl,
            "trailing_active":  False,
            "tp1_hit":          False,
            "be_active":        False,
            "open_time":        time.time(),
            "atr":              atr,
            "fraction":         fraction,
            "tier":             tier,
            "session_active":   info.get("session_active", True),
        }

        active, session = is_active_session()
        conf = info.get("confluence", "?")
        print(f"\n  ✅ [{symbol}] {side} @{entry:.5f} qty={qty} (size={fraction*100:.0f}% | tier={tier})")
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
#  PROGRESSIVE TRAILING CALCULATOR (v13 BARU)
# ════════════════════════════════════════════════════
def calc_progressive_trail(price, pos):
    """
    v13: Trail makin ketat saat mendekati TP2.
    Phase 1 (TP1→mid): 0.8x ATR — kasih ruang
    Phase 2 (mid→75%): 0.5x ATR — mulai protect
    Phase 3 (>75%):    0.3x ATR — lock aggresif
    """
    entry = pos["entry"]
    tp1   = pos["tp1"]
    tp2   = pos["tp2"]
    atr   = pos.get("atr", price * 0.005)
    side  = pos["side"]

    if side == "LONG":
        tp1_dist = tp2 - tp1
        progress = (price - tp1) / tp1_dist if tp1_dist > 0 else 0
    else:
        tp1_dist = tp1 - tp2
        progress = (tp1 - price) / tp1_dist if tp1_dist > 0 else 0

    progress = max(0, min(progress, 1))

    if progress < 0.50:
        mult = ATR_TRAIL_PHASE1
    elif progress < 0.75:
        mult = ATR_TRAIL_PHASE2
    else:
        mult = ATR_TRAIL_PHASE3

    if side == "LONG":
        return price - mult * atr
    else:
        return price + mult * atr

# ════════════════════════════════════════════════════
#  POSITION MANAGEMENT (v13)
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
        fee   = exit_price * TAKER_FEE * close_qty
        gross = (exit_price - pos["entry"]) * close_qty if side == "LONG" \
                else (pos["entry"] - exit_price) * close_qty
        net   = gross - fee - (pos["entry"] * TAKER_FEE * close_qty)
        pct   = net / (pos["entry"] * close_qty) * 100

        print(f"  🎯 [{symbol}] PARTIAL {reason} @{exit_price:.5f}")
        print(f"     💛 Gross: {gross:+.4f}U | Fee: {fee:.4f}U | Net: {net:+.4f}U ({pct:+.2f}%)")

        pos["tp1_hit"]          = True
        pos["qty_remain"]       = abs(amt) - close_qty
        pos["be_active"]        = True
        pos["sl"]               = pos["entry"] * (1 + ROUND_TRIP_FEE * 1.5) if side == "LONG" \
                                  else pos["entry"] * (1 - ROUND_TRIP_FEE * 1.5)
        pos["trailing_active"]  = True
        pos["peak"]             = exit_price
        # Progressive trailing setelah TP1 (v13)
        pos["trail_sl"]         = calc_progressive_trail(exit_price, pos)

        print(f"     🔒 BE stop @{pos['sl']:.5f} | Trail:{pos['trail_sl']:.5f} (progressive)")
        trade_log.append({"symbol": symbol, "side": side, "pnl": round(net, 4), "reason": f"Partial {reason}"})
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal partial: {e}")
        pos["tp1_hit"] = True

def close_trade(symbol, reason=""):
    global _consec_loss, _loss_weight, _in_cooldown
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
            fee   = (pos["entry"] + exit_) * TAKER_FEE * qty_r
            net   = gross - fee
            pct   = net / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
            emoji = "🟢" if net >= 0 else "🔴"
            be_tag = " [BE]" if pos.get("be_active") else ""
            hold_min = (time.time() - pos.get("open_time", time.time())) / 60

            print(f"  💰 [{symbol}] CLOSED {reason}{be_tag} ({hold_min:.0f}min | {pos['tier']})")
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
    global _consec_loss, _loss_weight, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        avg_win = get_avg_win()

        # v13: Weighted loss
        loss_magnitude = abs(pnl)
        if avg_win > 0 and loss_magnitude > avg_win * 2:
            weight = 2.0  # loss besar = +2 weight
            print(f"  ⚠️  [{symbol}] HEAVY LOSS ({loss_magnitude:.2f}U > 2x avg_win {avg_win:.2f}U)")
        elif loss_magnitude >= SIGNIFICANT_LOSS_USDT:
            weight = 1.5
        else:
            weight = 1.0
        _loss_weight += weight

        set_symbol_cooldown(symbol, loss_usdt=pnl)

        if _consec_loss >= GLOBAL_COOLDOWN_LOSS:
            btc_bad     = _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < 0.30
            if btc_bad and breadth_bad:
                _in_cooldown = True
                print(f"  🧊 {GLOBAL_COOLDOWN_LOSS} loss + market buruk → Global Cooldown!")
            else:
                print(f"  ⚡ {_consec_loss} loss tapi market oke — symbol cooldown")
                _consec_loss = 0
    else:
        # Win: reset loss counter, tapi loss_weight recovery lebih lambat
        _consec_loss = 0
        _loss_weight = max(0, _loss_weight - 0.5)  # recovery bertahap
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
        is_session_active = pos.get("session_active", True)

        # ── Emergency exits ────────────────────────────
        if _macro["news"] == "strong_negative":
            close_trade(symbol, "🚨 Emergency news"); continue
        if side == "LONG" and _macro["btc_trend_1h"] == "BEAR" and _macro["btc_trend_4h"] == "BEAR":
            close_trade(symbol, "⚡ BTC 1H+4H BEAR"); continue
        if side == "SHORT" and _macro["btc_trend_1h"] == "BULL" and _macro["btc_trend_4h"] == "BULL":
            close_trade(symbol, "⚡ BTC 1H+4H BULL"); continue

        # ── Time-based forced exit (v13 — session-aware) ──
        hold_mins = (time.time() - pos.get("open_time", time.time())) / 60
        max_hold  = MAX_HOLD_OFF_SESSION if not is_session_active else MAX_HOLD_MINUTES
        if hold_mins > max_hold and not pos["tp1_hit"]:
            close_trade(symbol, f"⏰ Max hold {max_hold}min"); continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            if not pos["tp1_hit"] and price >= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price - ATR_TRAIL_PHASE1 * atr
                print(f"  🔄 [{symbol}] Progressive Trail aktif @{price:.5f}")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                # Progressive trail: lebih ketat saat mendekati TP2
                new_trail = calc_progressive_trail(price, pos)
                pos["trail_sl"] = max(pos["trail_sl"], new_trail)

            if pos["tp1_hit"] and price >= pos["tp2"]:
                close_trade(symbol, "✨ TP2 hit"); continue

            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 Progressive Trail Stop"); continue

            if price <= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 SL"
                close_trade(symbol, reason); continue

            pnl_est = (price - entry) * pos.get("qty_remain", pos["qty"])
            tp_tag  = f"TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f"TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            be_tag  = " [BE]" if pos.get("be_active") else ""
            print(f"  📌 [{symbol}] LONG @{entry:.4f}→{price:.4f}{be_tag} | "
                  f"{pnl_est:+.3f}U{tsl} | {tp_tag} | {pos.get('tier','?')}")

        else:  # SHORT
            profit_pct = (entry - price) / entry

            if not pos["tp1_hit"] and price <= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price + ATR_TRAIL_PHASE1 * atr
                print(f"  🔄 [{symbol}] Progressive Trail aktif @{price:.5f}")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                new_trail = calc_progressive_trail(price, pos)
                pos["trail_sl"] = min(pos["trail_sl"], new_trail)

            if pos["tp1_hit"] and price <= pos["tp2"]:
                close_trade(symbol, "✨ TP2 hit"); continue

            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 Progressive Trail Stop"); continue

            if price >= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 SL"
                close_trade(symbol, reason); continue

            pnl_est = (entry - price) * pos.get("qty_remain", pos["qty"])
            tp_tag  = f"TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f"TP1:{pos['tp1']:.4f}"
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            be_tag  = " [BE]" if pos.get("be_active") else ""
            print(f"  📌 [{symbol}] SHORT @{entry:.4f}→{price:.4f}{be_tag} | "
                  f"{pnl_est:+.3f}U{tsl} | {tp_tag} | {pos.get('tier','?')}")

# ════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════
def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"] > 0)
    n     = len(trade_log)
    wr    = wins / n * 100 if n else 0
    be_wr = 1 / (1 + RR_RATIO) * 100
    cd_info = f" | 🧊 GlobalCD" if _in_cooldown else ""
    sym_cd  = f" | {len(_symbol_cooldown)}sym-cd" if _symbol_cooldown else ""
    lw_info = f" | LossW:{_loss_weight:.1f}" if _loss_weight > 0 else ""
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% (BE:{be_wr:.0f}%) | "
          f"W:{wins} L:{n-wins} | Net P&L:{total:+.4f}U{cd_info}{sym_cd}{lw_info}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:40]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    print("🤖 Bot v13 — Precision High-WR Scalping")
    print(f"   Leverage     : {LEVERAGE}x | Order: ${ORDER_USDT} USDT")
    print(f"   Fee          : {TAKER_FEE*100:.3f}% per sisi ({ROUND_TRIP_FEE*100:.3f}% round-trip)")
    print(f"   SL Cap       : LARGE≤2.2% | MID≤1.8% | SMALL≤1.2% (session-adjusted)")
    print(f"   RR           : OVERLAP={RR_OVERLAP} | ACTIVE={RR_ACTIVE} | OFF={RR_OFF}")
    print(f"   Score        : BASE={BASE_MIN_SCORE} | OFF session min={BASE_MIN_SCORE_OFF}")
    print(f"   Trailing     : Progressive (0.8→0.5→0.3x ATR saat mendekati TP2)")
    print(f"   Max hold     : {MAX_HOLD_MINUTES}min (active) | {MAX_HOLD_OFF_SESSION}min (off-session)")
    print(f"   Loss weight  : Kumulatif, decay 0.5 per win. Size auto-reduce.")
    print(f"   Small cap    : DIBLOKIR saat off-session")
    print(f"   Meme/HV      : DIBLOKIR saat off-session atau breadth <45%")
    print(f"   Sector corr  : Max 1 posisi per sektor")
    print(f"   Symbols      : {len(SYMBOLS)} coin\n")

    print("  ⏳ Validasi symbols & setup...")
    symbols = validate_symbols()
    for s in symbols[:20]: get_sym_info(s)
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
        lw_info = f" | LossW:{_loss_weight:.1f}" if _loss_weight > 0 else ""
        print(f"\n{'='*76}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | "
              f"USDT.D:{_macro['usdt_d']}% | News:{_macro['news']}{cd_info}")
        print(f"  📈 BTC: 15m={_macro['btc_trend_15m']} | 1H={_macro['btc_trend_1h']} | "
              f"4H={_macro['btc_trend_4h']}")
        print(f"  🌍 Breadth:{_macro['market_breadth']*100:.0f}% | MCap24h:{_macro['global_mcap_chg']:+.1f}% | "
              f"Session:{session}{sym_cd}{lw_info}")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}/{MAX_POSITIONS}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*76}")

        if len(open_positions) < MAX_POSITIONS and not _in_cooldown and \
           _macro["news"] != "strong_negative":

            allowed_dirs, quality_gate, ctx_reason = get_market_context()

            if not allowed_dirs:
                print(f"  🚫 SKIP SCAN — {ctx_reason}")
            else:
                gate_icon = "⭐" if quality_gate == "PREMIUM" else "✓"
                print(f"  🔍 Scanning {len(symbols)} symbols | {gate_icon} {quality_gate} | "
                      f"Dir:{'/'.join(sorted(allowed_dirs))} | {ctx_reason}")
                if not active:
                    print(f"  ⚠️  OFF session — min score={BASE_MIN_SCORE_OFF}, hanya LARGE/MID, size 70%")

                candidates, skipped = parallel_scan(symbols, allowed_dirs, quality_gate)

                if candidates:
                    candidates.sort(key=lambda x: x[5].get("score_num", 0), reverse=True)
                    print(f"\n  🎯 {len(candidates)} setup valid | {skipped} di-skip")
                    for sym, side, sl, tp1, tp2, info in candidates[:3]:
                        tier = info.get("tier", "?")
                        print(f"     ⭐ {sym}({tier}) {side} | Score:{info.get('score','?')} | "
                              f"SL:{info.get('sl_pct','?')} | R:R:{info.get('rr','?')}")
                    for sym, side, sl, tp1, tp2, info in candidates:
                        if len(open_positions) >= MAX_POSITIONS: break
                        open_trade(sym, side, sl, tp1, tp2, info)
                else:
                    print(f"  ⏳ {skipped} di-scan, belum ada setup valid")
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
