"""
Bot Trading v8 — Anti Bearish Entry + Smart Cooldown
======================================================
Fix dari v7:
- BTC trend filter: kalau BTC turun, SKIP semua LONG altcoin
- MIN_FNG naik ke 45 (lebih konservatif)
- Cooldown KONTEKSTUAL setelah 2 loss berturut-turut:
  * Cooldown aktif hanya kalau BTC masih BEAR/MILD_BEAR ATAU breadth masih < 40%
  * Cooldown otomatis batal kalau BTC balik BULL/MILD_BULL DAN breadth > 50%
  * Tidak pakai timer buta 30 menit — tergantung kondisi market
- Market breadth check: hitung berapa % coin yang turun
- Konfirmasi multi-timeframe lebih ketat (15m + 1H harus searah)
- SL lebih longgar (2x ATR) supaya tidak kena noise
- Tidak entry kalau semua sinyal cuma LONG di market yang merah

Update v8.1:
- SYMBOLS diperluas dari 28 → 128 token (top market cap)
- Mencakup: L1, L2, DeFi, AI/Data, Gaming/NFT, Meme, Mid-cap
"""

import os, time, math, requests
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import ta
import pandas as pd

load_dotenv()
client = Client(os.getenv("API_KEY"), os.getenv("API_SECRET"))
# Hapus baris berikut untuk akun REAL (uncomment untuk testnet):
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

# ════════════════════════════════════════════════════
#  CONFIG — sesuaikan dengan saldo kamu
# ════════════════════════════════════════════════════
LEVERAGE              = 10
ORDER_USDT            = 10       # ganti ke 2 untuk akun real $2.5
ATR_SL_MULT           = 2.0      # lebih longgar (was 1.5) — kurangi noise SL
ATR_TP_MULT           = 4.0      # RR 1:2
TRAIL_TRIGGER         = 0.006    # aktifkan trailing setelah +0.6%
TRAIL_PCT             = 0.003
MIN_TA_VOTES          = 7        # lebih ketat (was 6)
MIN_FNG               = 45       # lebih konservatif (was 35)
MAX_POSITIONS         = 2
SCAN_INTERVAL         = 60
MAX_CONSEC_LOSS       = 2        # cooldown setelah berapa loss berturut-turut
MIN_MARKET_BREADTH    = 0.45     # minimal 45% coin harus bullish sebelum LONG

# ── Smart Cooldown Thresholds ────────────────────────
# Cooldown aktif kalau salah satu kondisi ini terpenuhi setelah MAX_CONSEC_LOSS
COOLDOWN_BTC_BAD      = {"BEAR", "MILD_BEAR"}   # BTC trend yang dianggap "masih buruk"
COOLDOWN_BREADTH_MAX  = 0.40                    # breadth < 40% → market masih lemah
# Cooldown batal kalau KEDUA kondisi ini terpenuhi
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL"}   # BTC sudah balik positif
COOLDOWN_BREADTH_MIN  = 0.50                    # breadth sudah > 50%

# ════════════════════════════════════════════════════
#  SYMBOLS — 128 token top market cap
# ════════════════════════════════════════════════════
SYMBOLS = [
    # ── Original 28 ──────────────────────────────────────
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "MATICUSDT","LTCUSDT","ATOMUSDT","UNIUSDT","ETCUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT",
    "SUIUSDT","TIAUSDT","AAVEUSDT","RUNEUSDT","FILUSDT",
    "1000PEPEUSDT","WIFUSDT","JUPUSDT",

    # ── Layer 1 / Infrastructure ──────────────────────────
    "TRXUSDT","XLMUSDT","BCHUSDT","TONUSDT","VETUSDT",
    "ICPUSDT","HBARUSDT","STXUSDT","KASUSDT","ALGOUSDT",
    "XTZUSDT","ZECUSDT","DASHUSDT","EOSUSDT","NEOUSDT",
    "FLOWUSDT","QNTUSDT","IOTAUSDT","TAOUSDT","SEIUSDT",
    "DYMUSDT","MANTAUSDT","AKTUSDT","BEAMXUSDT","POLTUSDT",

    # ── DeFi ─────────────────────────────────────────────
    "LDOUSDT","CRVUSDT","MKRUSDT","SNXUSDT","COMPUSDT",
    "YFIUSDT","1INCHUSDT","SUSHIUSDT","BALUSDT","CAKEUSDT",
    "GMXUSDT","DYDXUSDT","STGUSDT","RDNTUSDT","ONDOUSDT",

    # ── AI / Data ─────────────────────────────────────────
    "FETUSDT","RENDERUSDT","AGIXUSDT","RNDRUSDT","OCEANUSDT",
    "AIUSDT","GLMUSDT","MOVRUSDT","EIGENUSDT","ENAUSDT",

    # ── Gaming / NFT / Metaverse ──────────────────────────
    "SANDUSDT","MANAUSDT","AXSUSDT","GALAUSDT","ENJUSDT",
    "CHZUSDT","IMXUSDT","MAGICUSDT","GMTUSDT","RONINUSDT",
    "XAIUSDT","PIXELUSDT","ACEUSDT","AGLDUSDT","BLURUSDT",

    # ── Layer 2 / Scaling ─────────────────────────────────
    "STRKUSDT","ZKUSDT","ZROUSDT","ALTUSDT","MNTUSDT",
    "WUSDT","PORTALUSDT","LISTAUSDT","IOUSDT","CELRUSDT",

    # ── Meme / Community ─────────────────────────────────
    "NOTUSDT","DOGSUSDT","CATUSDT","HMSTRUSDT","CATIUSDT",
    "GOATSUSDT","MOODENGUSDT","ORDIUSDT","SATSUSDT","WLDUSDT",

    # ── Misc / Mid-cap ────────────────────────────────────
    "ANKRUSDT","SKLUSDT","SXPUSDT","WAVESUSDT","PENGUUSDT",
    "LOOMUSDT","BNXUSDT","PYTHUSDT","PDAUSDT","MAGICUSDT",
]

open_positions  = {}
trade_log       = []
_last_candle    = {}
_consec_loss    = 0
_in_cooldown    = False   # flag cooldown kontekstual (bukan timer)

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
        valid  = {s["symbol"] for s in client.futures_exchange_info()["symbols"] if s["status"]=="TRADING"}
        result = list(dict.fromkeys([s for s in SYMBOLS if s in valid]))
        print(f"  ✅ {len(result)}/{len(SYMBOLS)} symbols valid di Binance Futures")
        invalid = [s for s in SYMBOLS if s not in valid]
        if invalid:
            print(f"  ⚠️  Di-skip (tidak tersedia): {', '.join(invalid)}")
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
            "time","open","high","low","close","volume",
            "ct","qv","trades","tbbase","tbquote","ignore"])
        for c in ["open","high","low","close","volume","tbbase","tbquote"]:
            df[c] = df[c].astype(float)
        df["time"] = pd.to_numeric(df["time"])
        return df
    except: return None

# ════════════════════════════════════════════════════
#  MACRO CACHE
# ════════════════════════════════════════════════════
_macro = {
    "fng":50, "fng_label":"Neutral",
    "usdt_d":5.0, "usdt_prev":5.0,
    "news":"neutral", "headlines":[],
    "btc_trend":"UNKNOWN",
    "market_breadth": 0.5,
    "last_fng":0, "last_dom":0, "last_news":0,
    "last_btc":0, "last_breadth":0
}

def refresh_macro():
    now = time.time()

    # Fear & Greed
    if now - _macro["last_fng"] > 300:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1",timeout=5).json()["data"][0]
            _macro["fng"]       = int(d["value"])
            _macro["fng_label"] = d["value_classification"]
            _macro["last_fng"]  = now
        except: pass

    # USDT Dominance
    if now - _macro["last_dom"] > 300:
        try:
            d = requests.get("https://api.coingecko.com/api/v3/global",timeout=8).json()
            _macro["usdt_prev"] = _macro["usdt_d"]
            _macro["usdt_d"]    = round(d["data"]["market_cap_percentage"].get("usdt",5),2)
            _macro["last_dom"]  = now
        except: pass

    # News tiap 60 detik
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
                t  = post.get("title","")
                tl = t.lower()
                if any(w in tl for w in neg_kw):   neg+=1; hl.append(f"🔴 {t[:55]}")
                elif any(w in tl for w in pos_kw): pos+=1; hl.append(f"🟢 {t[:55]}")
            _macro["news"]      = "negative" if neg>=2 else ("positive" if pos>=2 else "neutral")
            _macro["headlines"] = hl[:3]
            _macro["last_news"] = now
        except: pass

    # BTC Trend tiap 60 detik — INI YANG PENTING
    if now - _macro["last_btc"] > 60:
        try:
            df = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_15MINUTE, 50)
            if df is not None and len(df) >= 30:
                c     = df["close"]
                price = c.iloc[-1]
                ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
                ema21 = ta.trend.EMAIndicator(c, 21).ema_indicator().iloc[-1]
                ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator().iloc[-1]
                # Cek perubahan harga 4 candle terakhir (1 jam)
                chg_1h = (price - c.iloc[-4]) / c.iloc[-4] * 100

                if price > ema9 > ema21 > ema50 and chg_1h > 0:
                    _macro["btc_trend"] = "BULL"
                elif price < ema9 < ema21 < ema50 and chg_1h < 0:
                    _macro["btc_trend"] = "BEAR"
                elif price > ema21 and chg_1h > -0.3:
                    _macro["btc_trend"] = "MILD_BULL"
                elif price < ema21 and chg_1h < 0.3:
                    _macro["btc_trend"] = "MILD_BEAR"
                else:
                    _macro["btc_trend"] = "SIDEWAYS"
            _macro["last_btc"] = now
        except: pass

    # Market Breadth tiap 5 menit — berapa % coin yang bullish
    if now - _macro["last_breadth"] > 300:
        try:
            bullish = 0
            sample  = SYMBOLS[:15]   # cek 15 coin saja biar cepat
            for sym in sample:
                df = get_ohlcv(sym, Client.KLINE_INTERVAL_15MINUTE, 10)
                if df is not None and len(df) >= 5:
                    # Bullish = harga di atas EMA9 dan candle terakhir hijau
                    c  = df["close"]
                    ema9 = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
                    if c.iloc[-1] > ema9 and df["close"].iloc[-1] > df["open"].iloc[-1]:
                        bullish += 1
            _macro["market_breadth"] = bullish / len(sample)
            _macro["last_breadth"]   = now
        except: pass

# ════════════════════════════════════════════════════
#  SMART COOLDOWN LOGIC
# ════════════════════════════════════════════════════
def check_cooldown_recover():
    """
    Cek apakah kondisi market sudah membaik dan cooldown bisa dibatalkan.
    Cooldown batal kalau BTC sudah recover DAN market breadth sudah cukup tinggi.
    Returns True kalau cooldown harus dibatalkan.
    """
    btc_ok      = _macro["btc_trend"] in COOLDOWN_BTC_RECOVER
    breadth_ok  = _macro["market_breadth"] >= COOLDOWN_BREADTH_MIN
    return btc_ok and breadth_ok

def is_cooldown_active():
    """
    Cooldown aktif kalau:
    1. Flag _in_cooldown True, DAN
    2. Market masih buruk: BTC masih BEAR/MILD_BEAR ATAU breadth masih < 40%
    Kalau market sudah recover → batalkan cooldown otomatis.
    """
    global _in_cooldown
    if not _in_cooldown:
        return False
    # Cek apakah market sudah recover
    if check_cooldown_recover():
        _in_cooldown = False
        print(f"  ✅ Cooldown dibatalkan! BTC:{_macro['btc_trend']} Breadth:{_macro['market_breadth']*100:.0f}% — market sudah recover")
        return False
    return True

def cooldown_reason():
    """Jelaskan kenapa cooldown masih aktif."""
    reasons = []
    if _macro["btc_trend"] in COOLDOWN_BTC_BAD:
        reasons.append(f"BTC masih {_macro['btc_trend']}")
    if _macro["market_breadth"] < COOLDOWN_BREADTH_MIN:
        reasons.append(f"breadth {_macro['market_breadth']*100:.0f}% < {COOLDOWN_BREADTH_MIN*100:.0f}%")
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
    lv = sv = 0

    # 1. RSI
    rsi = last["rsi"]
    if rsi < 35:   lv += 1
    elif rsi > 65: sv += 1

    # 2. RSI fast
    rf = last["rsi_fast"]
    if rf < 30:   lv += 1
    elif rf > 70: sv += 1

    # 3. MACD fresh crossover
    if (last["macd"] > last["macd_sig"] and prev["macd"] <= prev["macd_sig"]) or \
       (prev["macd"] > prev["macd_sig"] and p2["macd"] <= p2["macd_sig"]):
        lv += 1
    elif (last["macd"] < last["macd_sig"] and prev["macd"] >= prev["macd_sig"]) or \
         (prev["macd"] < prev["macd_sig"] and p2["macd"] >= p2["macd_sig"]):
        sv += 1

    # 4. EMA stack
    if last["ema9"] > last["ema21"] > last["ema50"]:   lv += 1
    elif last["ema9"] < last["ema21"] < last["ema50"]: sv += 1

    # 5. EMA200
    if not pd.isna(last["ema200"]):
        if last["close"] > last["ema200"]: lv += 1
        else:                              sv += 1

    # 6. Bollinger
    price = last["close"]
    if price <= last["bb_lo"] * 1.002:   lv += 1
    elif price >= last["bb_hi"] * 0.998: sv += 1

    # 7. Stochastic crossover
    k, d   = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    if k < 25 and k > d and pk <= pd_:   lv += 1
    elif k > 75 and k < d and pk >= pd_: sv += 1

    # 8. Taker buy ratio
    br = last["buy_ratio"]
    if br > 0.60:   lv += 1
    elif br < 0.40: sv += 1

    # 9. Volume
    if last["vol_ratio"] > 1.2:
        if last["close"] > last["open"]: lv += 1
        else:                            sv += 1

    # 10. Candle body
    if last["body_ratio"] > 0.6:
        if last["close"] > last["open"]: lv += 1
        else:                            sv += 1

    # Penalti regime
    if regime == "BULL"  and sv > lv: return "NONE", lv, sv
    if regime == "BEAR"  and lv > sv: return "NONE", lv, sv
    if regime == "RANGE" and max(lv,sv) < 7: return "NONE", lv, sv

    if lv >= MIN_TA_VOTES and lv > sv + 1: return "LONG",  lv, sv
    if sv >= MIN_TA_VOTES and sv > lv + 1: return "SHORT", lv, sv
    return "NONE", lv, sv

# ════════════════════════════════════════════════════
#  ORDER BOOK + CUMULATIVE DELTA + WHALE
# ════════════════════════════════════════════════════
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
        return ("mild_buy"  if last["close"] > last["open"] else "mild_sell"), ratio
    return "none", ratio

def get_funding(symbol):
    try:
        data = client.futures_funding_rate(symbol=symbol, limit=1)
        return round(float(data[0]["fundingRate"])*100, 4)
    except: return 0.0

# ════════════════════════════════════════════════════
#  MASTER ENTRY FILTER
# ════════════════════════════════════════════════════
def should_enter(symbol, df):
    info = {}

    # ── 1. Smart Cooldown check ───────────────────────────────
    # Cooldown kontekstual: aktif sampai market recover, bukan timer buta
    if is_cooldown_active():
        return None, 0, 0, {"skip": f"🧊 Cooldown aktif ({cooldown_reason()})"}

    # ── 2. Macro hard blocks ──────────────────────────────────
    fng     = _macro["fng"]
    news    = _macro["news"]
    usdt_up = _macro["usdt_d"] > _macro["usdt_prev"] + 0.05  # harus naik signifikan

    if fng < MIN_FNG:
        return None, 0, 0, {"skip": f"F&G terlalu rendah ({fng})"}
    if news == "negative":
        return None, 0, 0, {"skip": "News buruk"}
    if usdt_up:
        return None, 0, 0, {"skip": "USDT.D naik (risk-off)"}

    # ── 3. BTC Trend filter — KUNCI ANTI BEARISH ENTRY ───────
    btc_trend = _macro["btc_trend"]
    info["btc"] = btc_trend

    # ── 4. Market Breadth ─────────────────────────────────────
    breadth = _macro["market_breadth"]
    info["breadth"] = f"{breadth*100:.0f}%"

    # ── 5. Regime per symbol ──────────────────────────────────
    regime = get_regime(symbol)
    info["regime"] = regime

    # ── 6. Candle timing ──────────────────────────────────────
    prev_candle_time = int(df["time"].iloc[-2])
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 60:
        return None, 0, 0, {"skip": "Data tidak cukup"}
    if _last_candle.get(symbol) == prev_candle_time:
        return None, 0, 0, {"skip": "Sudah dianalisa candle ini"}

    # ── 7. Technical Analysis ─────────────────────────────────
    df_closed = run_ta(df_closed)
    ta_dir, lv, sv = get_ta_votes(df_closed, regime)
    info["ta"] = f"{ta_dir} L:{lv}/S:{sv}"
    if ta_dir == "NONE":
        return None, 0, 0, {"skip": f"TA lemah (L:{lv} S:{sv})"}

    # ── 8. BTC + Breadth vs arah trade ───────────────────────
    if ta_dir == "LONG":
        if btc_trend == "BEAR":
            return None, 0, 0, {"skip": "BTC BEAR — tidak LONG altcoin"}
        if btc_trend == "MILD_BEAR":
            return None, 0, 0, {"skip": "BTC MILD_BEAR — skip LONG"}
        if breadth < MIN_MARKET_BREADTH:
            return None, 0, 0, {"skip": f"Market breadth rendah ({breadth*100:.0f}% bullish)"}
        if regime == "BEAR":
            return None, 0, 0, {"skip": "Regime symbol BEAR — tidak LONG"}

    if ta_dir == "SHORT":
        if btc_trend == "BULL":
            return None, 0, 0, {"skip": "BTC BULL — tidak SHORT altcoin"}
        if btc_trend == "MILD_BULL":
            return None, 0, 0, {"skip": "BTC MILD_BULL — skip SHORT"}
        if breadth > 0.65:
            return None, 0, 0, {"skip": f"Market breadth tinggi ({breadth*100:.0f}% bullish), skip SHORT"}
        if regime == "BULL":
            return None, 0, 0, {"skip": "Regime symbol BULL — tidak SHORT"}

    # ── 9. Order Book ─────────────────────────────────────────
    ob_imb = get_ob_imbalance(symbol)
    info["ob"] = ob_imb
    if ta_dir == "LONG"  and ob_imb < -0.1:
        return None, 0, 0, {"skip": "OB melawan LONG"}
    if ta_dir == "SHORT" and ob_imb >  0.1:
        return None, 0, 0, {"skip": "OB melawan SHORT"}

    # ── 10. Cumulative Delta ──────────────────────────────────
    cum_d = get_cum_delta(df_closed)
    info["cum_delta"] = cum_d
    if ta_dir == "LONG"  and cum_d < -0.15:
        return None, 0, 0, {"skip": "CumDelta bearish"}
    if ta_dir == "SHORT" and cum_d >  0.15:
        return None, 0, 0, {"skip": "CumDelta bullish"}

    # ── 11. Whale ─────────────────────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    info["whale"] = f"{whale_dir}({whale_ratio:.1f}x)"
    if ta_dir == "LONG"  and whale_dir == "sell_whale":
        return None, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, {"skip": "Whale buy aktif"}

    # ── 12. Funding Rate ──────────────────────────────────────
    funding = get_funding(symbol)
    info["funding"] = funding
    if ta_dir == "LONG"  and funding >  0.1:
        return None, 0, 0, {"skip": "Funding terlalu positif (long squeeze risk)"}
    if ta_dir == "SHORT" and funding < -0.1:
        return None, 0, 0, {"skip": "Funding terlalu negatif (short squeeze risk)"}

    # ── 13. BB Width (anti choppy) ────────────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    info["bb_width"] = round(bb_width, 4)
    if bb_width < 0.01:
        return None, 0, 0, {"skip": "BB terlalu sempit, choppy"}

    # ── 14. ATR-based SL/TP ───────────────────────────────────
    atr   = df_closed["atr"].iloc[-1]
    price = df_closed["close"].iloc[-1]
    if ta_dir == "LONG":
        sl_price = round(price - ATR_SL_MULT * atr, 8)
        tp_price = round(price + ATR_TP_MULT * atr, 8)
    else:
        sl_price = round(price + ATR_SL_MULT * atr, 8)
        tp_price = round(price - ATR_TP_MULT * atr, 8)

    sl_pct = abs(price - sl_price) / price
    if sl_pct > 0.04:   # naikkan dari 3% ke 4%
        return None, 0, 0, {"skip": f"ATR terlalu besar (SL={sl_pct*100:.1f}%)"}

    _last_candle[symbol] = prev_candle_time
    info["atr_sl_pct"] = f"{sl_pct*100:.2f}%"
    return ta_dir, sl_price, tp_price, info

# ════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════
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
        print(f"     BTC:{info.get('btc','?')} regime:{info.get('regime','?')} breadth:{info.get('breadth','?')}")
        print(f"     TA:{info.get('ta','?')} OB:{info.get('ob',0):+.2f} Δ:{info.get('cum_delta',0):+.3f} 🐋:{info.get('whale','?')}")
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal entry: {e}")

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
                    pnl = (exit_-pos["entry"])*pos["qty"] if pos["side"]=="LONG" \
                          else (pos["entry"]-exit_)*pos["qty"]
                    pct = pnl/(pos["entry"]*pos["qty"])*100
                    print(f"  ⚠️  [{symbol}] Sudah tutup di exchange")
                    print(f"     {'🟢' if pnl>=0 else '🔴'} Est P&L: {pnl:+.4f} USDT ({pct:+.2f}%)")
                    trade_log.append({"symbol":symbol,"side":pos["side"],
                                      "pnl":round(pnl,4),"reason":"External close"})
                    _update_loss_streak(pnl)
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
            _update_loss_streak(pnl)

        open_positions.pop(symbol, None)
        return True
    except Exception as e:
        print(f"  ❌ [{symbol}] Gagal close: {e}")
        return False

def _update_loss_streak(pnl):
    """
    Update consecutive loss counter dan aktifkan/reset cooldown kontekstual.
    Cooldown aktif kalau sudah MAX_CONSEC_LOSS loss DAN market memang buruk.
    Kalau market sudah bagus, cooldown tidak diaktifkan meski loss streak.
    """
    global _consec_loss, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        if _consec_loss >= MAX_CONSEC_LOSS:
            # Cek kondisi market sekarang
            btc_bad     = _macro["btc_trend"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < COOLDOWN_BREADTH_MAX
            if btc_bad or breadth_bad:
                _in_cooldown = True
                reasons = []
                if btc_bad:     reasons.append(f"BTC {_macro['btc_trend']}")
                if breadth_bad: reasons.append(f"breadth {_macro['market_breadth']*100:.0f}%")
                print(f"  🧊 {MAX_CONSEC_LOSS} loss berturut-turut + market buruk ({', '.join(reasons)}) → Cooldown aktif!")
                print(f"     Cooldown akan batal otomatis kalau BTC recover ke BULL/MILD_BULL DAN breadth > {COOLDOWN_BREADTH_MIN*100:.0f}%")
            else:
                # Market masih bagus → tidak cooldown, reset saja streak-nya
                print(f"  ⚡ {MAX_CONSEC_LOSS} loss berturut-turut TAPI market masih bagus (BTC:{_macro['btc_trend']} breadth:{_macro['market_breadth']*100:.0f}%) → lanjut trading!")
                _consec_loss = 0   # reset agar tidak terus trigger
    else:
        _consec_loss = 0
        if _in_cooldown:
            # Win setelah cooldown → reset
            _in_cooldown = False
            print(f"  ✅ Win! Cooldown diakhiri.")

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

        if _macro["news"] == "negative":
            close_trade(symbol, "📰 Emergency — bad news")
            continue

        # BTC tiba-tiba BEAR saat posisi LONG — exit darurat
        if side == "LONG" and _macro["btc_trend"] == "BEAR":
            close_trade(symbol, "⚡ BTC jadi BEAR — exit LONG")
            continue

        # BTC tiba-tiba BULL saat posisi SHORT — exit darurat
        if side == "SHORT" and _macro["btc_trend"] == "BULL":
            close_trade(symbol, "⚡ BTC jadi BULL — exit SHORT")
            continue

        if side == "LONG":
            profit_pct = (price - entry) / entry
            if profit_pct >= TRAIL_TRIGGER and not pos["trailing_active"]:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @ {price:.5f} (+{profit_pct*100:.2f}%)")
            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
            if price >= pos["tp"]:
                close_trade(symbol, "✨ TAKE PROFIT"); continue
            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue
            if price <= pos["sl"]:
                close_trade(symbol, "🛑 STOP LOSS"); continue
            pnl_now = (price - entry) * pos["qty"]
            tsl = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] LONG @{entry:.4f} → {price:.4f} | {pnl_now:+.3f}U{tsl}")

        else:
            profit_pct = (entry - price) / entry
            if profit_pct >= TRAIL_TRIGGER and not pos["trailing_active"]:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
                print(f"  🔄 [{symbol}] Trailing aktif @ {price:.5f} (+{profit_pct*100:.2f}%)")
            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
            if price <= pos["tp"]:
                close_trade(symbol, "✨ TAKE PROFIT"); continue
            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue
            if price >= pos["sl"]:
                close_trade(symbol, "🛑 STOP LOSS"); continue
            pnl_now = (entry - price) * pos["qty"]
            tsl = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            print(f"  📌 [{symbol}] SHORT @{entry:.4f} → {price:.4f} | {pnl_now:+.3f}U{tsl}")

# ════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════
def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"]>0)
    n     = len(trade_log)
    wr    = wins/n*100 if n else 0
    cd    = f" | 🧊 Cooldown ({cooldown_reason()})" if _in_cooldown else ""
    print(f"\n  📊 {n} trades | WR:{wr:.0f}% W:{wins} L:{n-wins} | P&L:{total:+.4f}U | streak:{_consec_loss}L{cd}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"]>0 else "🔴"
        print(f"     {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:35]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    print("🤖 Bot v8.1 — Anti Bearish Entry + Smart Cooldown + 128 Symbols")
    print(f"   Leverage      : {LEVERAGE}x | Order: ${ORDER_USDT} USDT")
    print(f"   SL/TP         : {ATR_SL_MULT}x/{ATR_TP_MULT}x ATR (RR 1:2)")
    print(f"   Trailing      : aktif setelah +{TRAIL_TRIGGER*100}%")
    print(f"   Min Votes     : {MIN_TA_VOTES}/10 | Min F&G: {MIN_FNG}")
    print(f"   BTC Filter    : BEAR/MILD_BEAR → skip LONG")
    print(f"   Market Breadth: min {MIN_MARKET_BREADTH*100:.0f}% coin bullish untuk LONG")
    print(f"   Smart Cooldown: aktif setelah {MAX_CONSEC_LOSS} loss JIKA market buruk")
    print(f"                   batal otomatis kalau BTC recover + breadth > {COOLDOWN_BREADTH_MIN*100:.0f}%")
    print(f"   Max Posisi    : {MAX_POSITIONS}")
    print(f"   Total Symbols : {len(SYMBOLS)} (sebelum validasi)\n")

    print("  ⏳ Setup & validasi symbols...")
    symbols = validate_symbols()
    for s in symbols: get_sym_info(s)
    refresh_macro()
    print(f"  ✅ {len(symbols)} symbols aktif | F&G:{_macro['fng']} | BTC:{_macro['btc_trend']} | News:{_macro['news']}\n")

    cycle = 0
    while True:
        cycle += 1
        refresh_macro()

        # Cek cooldown recover di setiap cycle (bukan hanya saat entry)
        if _in_cooldown:
            check_cooldown_recover()  # akan print notif kalau recover

        manage_positions()

        cd_info = f" 🧊 COOLDOWN ({cooldown_reason()})" if _in_cooldown else ""
        print(f"\n{'='*68}")
        print(f"  🔄 #{cycle} {time.strftime('%H:%M:%S')} | F&G:{_macro['fng']}({_macro['fng_label']}) | USDT:{_macro['usdt_d']}% | News:{_macro['news']}{cd_info}")
        print(f"  📈 BTC:{_macro['btc_trend']} | Breadth:{_macro['market_breadth']*100:.0f}% bullish")
        for h in _macro["headlines"]: print(f"  {h}")
        print(f"  📂 Posisi({len(open_positions)}): {list(open_positions.keys()) or '-'}")
        print(f"{'='*68}")

        skipped    = 0
        candidates = []

        if len(open_positions) < MAX_POSITIONS and _macro["news"] != "negative" \
           and not _in_cooldown:
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
                    print(f"     ⭐ {sym} {side} | {info.get('ta','?')} | OB:{info.get('ob',0):+.2f} | BTC:{info.get('btc','?')} | breadth:{info.get('breadth','?')}")
                for sym, side, sl, tp, info in candidates:
                    if len(open_positions) >= MAX_POSITIONS: break
                    open_trade(sym, side, sl, tp, info)
            else:
                print(f"  ⏳ {skipped} coins di-scan, belum ada setup valid")
        else:
            if _in_cooldown:
                print(f"  🧊 Cooldown aktif — {cooldown_reason()}")
                print(f"     Akan lanjut kalau BTC → BULL/MILD_BULL DAN breadth > {COOLDOWN_BREADTH_MIN*100:.0f}%")
            else:
                print(f"  ⏸️  Posisi penuh atau kondisi tidak aman")

        print_summary()
        print(f"\n  ⏱️  {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    run_bot()
