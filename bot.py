"""
Bot Trading v11 — Smart Aggressive Scalping (PRODUCTION READY)
==============================================================

PERBAIKAN dari v10:
  ✅ FIX #1: Daily Loss Limit -12% — stop semua trading kalau rugi melebihi batas
  ✅ FIX #2: calc_qty berdasarkan balance AKTUAL, bukan fixed ORDER_USDT
  ✅ FIX #3: get_5m_confirmation gagal fetch → return False (bukan lolos)
  ✅ FIX #4: OB Imbalance bobot turun 12→6, dipindah ke indikator lebih reliable
  ✅ FIX #5: Logging ke file (trading_bot_v11.log) + console
  ✅ FIX #6: CryptoPanic pakai API key proper + robust fallback
  ✅ FIX #7: S/R detection window diperbesar (2→4 candle) + volume filter
  ✅ FIX #8: _last_candle aman dari restart (persist ke JSON kecil)
  ✅ FIX #9: Rate limit protection + retry dengan exponential backoff
  ✅ FIX #10: Balance check sebelum setiap entry

FITUR BARU v11:
  🔔 TELEGRAM NOTIF: Open posisi, TP, SL, daily summary otomatis
  🪙 TOP 100 COINS: Auto-fetch dari Binance Futures (exclude micro-cap & meme)
  📊 BALANCE AWARE: Semua sizing berdasarkan balance real-time
  📁 FILE LOGGING: Semua event tercatat di log file
  🔄 API RETRY: Exponential backoff untuk semua Binance API call
  💹 RISK per trade: Max 1% balance per trade (bukan fixed USDT)

CARA SETUP TELEGRAM:
  1. Buka Telegram, cari @BotFather
  2. Ketik /newbot → ikuti instruksi → dapat TOKEN
  3. Cari @userinfobot atau ketik /start ke bot lu → dapat CHAT_ID
  4. Isi TELEGRAM_TOKEN dan TELEGRAM_CHAT_ID di .env

FILE .env yang dibutuhkan:
  API_KEY=<binance_api_key>
  API_SECRET=<binance_api_secret>
  TELEGRAM_TOKEN=<telegram_bot_token>
  TELEGRAM_CHAT_ID=<telegram_chat_id>
  CRYPTOPANIC_KEY=<optional, kosongkan kalau tidak punya>

INSTALL DEPENDENCIES:
  pip install python-binance ta pandas requests python-dotenv
"""

import os, time, math, json, logging, requests, traceback
from datetime import datetime, timezone
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import ta
import pandas as pd

load_dotenv()

# ════════════════════════════════════════════════════
#  LOGGING SETUP (file + console)
# ════════════════════════════════════════════════════
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("BotV11")
logger.setLevel(logging.DEBUG)

# File handler
fh = logging.FileHandler("trading_bot_v11.log", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(log_formatter)
logger.addHandler(fh)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(log_formatter)
logger.addHandler(ch)

def log(msg, level="info"):
    getattr(logger, level)(msg)

# ════════════════════════════════════════════════════
#  BINANCE CLIENT SETUP
# ════════════════════════════════════════════════════
client = Client(os.getenv("API_KEY"), os.getenv("API_SECRET"))

# ⚠️ HAPUS BARIS INI UNTUK AKUN REAL (MAINNET):
# client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

# ════════════════════════════════════════════════════
#  TELEGRAM SETUP
# ════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CRYPTOPANIC_KEY  = os.getenv("CRYPTOPANIC_KEY", "")

_tg_last_fail = 0
_tg_fail_count = 0

def send_telegram(msg: str, parse_mode="HTML"):
    """Kirim notif Telegram. Gagal = log warning, bot tetap jalan."""
    global _tg_last_fail, _tg_fail_count
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    # Skip kalau baru saja gagal (cooldown 60s)
    if time.time() - _tg_last_fail < 60 and _tg_fail_count >= 3:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": parse_mode,
        }, timeout=8)
        if resp.status_code == 200:
            _tg_fail_count = 0
        else:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        _tg_last_fail  = time.time()
        _tg_fail_count += 1
        log(f"[Telegram] Gagal kirim: {e}", "warning")

def tg_open(symbol, side, entry, sl, tp1, tp2, score, session, qty, est_pnl_sl):
    emoji = "🟢📈" if side == "LONG" else "🔴📉"
    sl_pct  = abs(entry - sl)   / entry * 100
    tp1_pct = abs(tp1  - entry) / entry * 100
    tp2_pct = abs(tp2  - entry) / entry * 100
    msg = (
        f"{emoji} <b>OPEN {side} — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Entry  : <code>{entry:.5f}</code>\n"
        f"🛑 SL     : <code>{sl:.5f}</code> (-{sl_pct:.2f}%)\n"
        f"🎯 TP1    : <code>{tp1:.5f}</code> (+{tp1_pct:.2f}%) [50%]\n"
        f"✨ TP2    : <code>{tp2:.5f}</code> (+{tp2_pct:.2f}%) [50%]\n"
        f"📦 Qty    : {qty}\n"
        f"📊 Score  : {score}\n"
        f"🕐 Session: {session}\n"
        f"⚠️ Est.Loss SL: <code>{est_pnl_sl:+.2f} USDT</code>"
    )
    send_telegram(msg)

def tg_close(symbol, side, entry, exit_price, pnl, pct, reason, balance):
    emoji = "🟢✅" if pnl >= 0 else "🔴❌"
    msg = (
        f"{emoji} <b>CLOSE {side} — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Entry  : <code>{entry:.5f}</code>\n"
        f"📤 Exit   : <code>{exit_price:.5f}</code>\n"
        f"💵 P&L    : <code>{pnl:+.4f} USDT</code> ({pct:+.2f}%)\n"
        f"📋 Reason : {reason}\n"
        f"💼 Balance: <code>{balance:.2f} USDT</code>"
    )
    send_telegram(msg)

def tg_daily_summary(trades, total_pnl, win_rate, balance, daily_loss_pct):
    emoji = "🟢" if total_pnl >= 0 else "🔴"
    msg = (
        f"📊 <b>DAILY SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} Total P&L : <code>{total_pnl:+.4f} USDT</code>\n"
        f"🎯 Win Rate : {win_rate:.1f}%\n"
        f"📈 Trades   : {trades}\n"
        f"📉 Daily Loss: {daily_loss_pct:+.2f}%\n"
        f"💼 Balance  : <code>{balance:.2f} USDT</code>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    send_telegram(msg)

def tg_daily_limit_hit(balance, loss_pct):
    msg = (
        f"🚨 <b>DAILY LOSS LIMIT HIT!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 Loss hari ini: <code>{loss_pct:.2f}%</code>\n"
        f"💼 Balance sisa : <code>{balance:.2f} USDT</code>\n"
        f"⛔ Bot BERHENTI trading hari ini.\n"
        f"🔄 Reset otomatis besok 00:00 UTC."
    )
    send_telegram(msg)

def tg_alert(msg):
    send_telegram(f"⚠️ <b>ALERT</b>\n{msg}")

# ════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════
LEVERAGE            = 10
RISK_PER_TRADE_PCT  = 0.01    # 1% balance per trade (bukan fixed USDT)
MAX_ORDER_USDT      = 60      # cap maksimum per order
MIN_ORDER_USDT      = 10      # minimum order
ATR_SL_MULT         = 1.5
ATR_TP1_MULT        = 1.5
ATR_TP2_MULT        = 3.0
TRAIL_TRIGGER       = 0.003
TRAIL_PCT           = 0.002
MAX_POSITIONS       = 3
SCAN_INTERVAL       = 45

# ── DAILY LOSS LIMIT ────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.12   # -12% dari balance awal hari ini → stop

# ── ADAPTIVE SCORE ──────────────────────────────────
BASE_MIN_SCORE        = 52
SCORE_BONUS_BTC_ALIGN = 8
SCORE_BONUS_VOL_SPIKE = 5
SCORE_BONUS_REGIME    = 5
SCORE_BONUS_MOMENTUM  = 5
OFF_SESSION_PENALTY   = 8

# ── FEAR & GREED ────────────────────────────────────
MIN_FNG               = 25
MAX_FNG_LONG          = 88
MIN_FNG_ANY           = 15
FNG_FEAR_ZONE         = 40
FNG_GREED_ZONE        = 75

# ── MACRO ───────────────────────────────────────────
USDT_RISK_OFF_DELTA   = 0.05
MIN_MARKET_BREADTH    = 0.35
SR_BUFFER             = 0.004

# ── VOLUME ──────────────────────────────────────────
MIN_VOLUME_SPIKE      = 1.3
MOMENTUM_CANDLES      = 5

# ── COOLDOWN ────────────────────────────────────────
MAX_CONSEC_LOSS       = 3
SYMBOL_COOLDOWN_SECS  = 300
GLOBAL_COOLDOWN_LOSS  = 4
COOLDOWN_BTC_BAD      = {"BEAR"}
COOLDOWN_BTC_RECOVER  = {"BULL", "MILD_BULL", "SIDEWAYS"}
COOLDOWN_BREADTH_MAX  = 0.30
COOLDOWN_BREADTH_MIN  = 0.40

# ── SESSIONS (UTC) ──────────────────────────────────
ACTIVE_SESSIONS = {
    "ASIA_OPEN": (0, 4),
    "EU_OPEN":   (7, 11),
    "NY_OPEN":   (13, 17),
    "OVERLAP":   (13, 15),
}

# ── SCORE WEIGHTS (OB dikurangi, RSI+EMA dinaikkan) ─
SCORE_WEIGHTS = {
    "macd_hist":    18,
    "rsi":          16,   # +1
    "ema_stack":    15,   # +1
    "volume":       13,
    "cum_delta":    12,   # +2 (lebih reliable dari OB)
    "ob_imbalance":  6,   # was 12 — dikurangi karena tidak reliable
    "stoch":         9,   # +1
    "bb":            7,   # +1
    "funding":       4,
}

# ── MEME/MICRO-CAP BLACKLIST ────────────────────────
BLACKLIST_KEYWORDS = [
    "PEPE","SHIB","DOGE","FLOKI","BONK","WIF","MEME",
    "BABYDOGE","ELON","SAFEMOON","SQUID","CAT","LADYS",
    "TURBO","WOJAK","COPE","BOME","MEW","NEIRO","PNUT",
    "ACT","LUNC","LUNA","ICP","TRX",  # volatile/manipulated
]

# ════════════════════════════════════════════════════
#  STATE
# ════════════════════════════════════════════════════
open_positions   = {}
trade_log        = []
_last_candle     = {}
_consec_loss     = 0
_in_cooldown     = False
_symbol_cooldown = {}
_sym_info        = {}

# Daily loss tracking
_daily_start_balance = 0.0
_daily_pnl           = 0.0
_daily_loss_hit      = False
_daily_reset_day     = -1
_last_summary_hour   = -1

# Retry state
_api_retry_delays    = {}

BULL_TRENDS = {"BULL", "MILD_BULL"}
BEAR_TRENDS = {"BEAR", "MILD_BEAR"}

# ════════════════════════════════════════════════════
#  LAST CANDLE PERSISTENCE (aman dari restart)
# ════════════════════════════════════════════════════
LAST_CANDLE_FILE = "last_candle_state.json"

def load_last_candle():
    global _last_candle
    try:
        if os.path.exists(LAST_CANDLE_FILE):
            with open(LAST_CANDLE_FILE, "r") as f:
                _last_candle = json.load(f)
            log(f"Loaded {len(_last_candle)} last_candle states")
    except Exception as e:
        log(f"last_candle load error: {e}", "warning")
        _last_candle = {}

def save_last_candle():
    try:
        with open(LAST_CANDLE_FILE, "w") as f:
            json.dump(_last_candle, f)
    except Exception as e:
        log(f"last_candle save error: {e}", "warning")

# ════════════════════════════════════════════════════
#  API RETRY (exponential backoff)
# ════════════════════════════════════════════════════
def api_call_with_retry(func, *args, max_retries=3, base_delay=1.0, **kwargs):
    """
    Wrapper untuk semua Binance API call.
    Retry dengan exponential backoff kalau kena rate limit atau error sementara.
    """
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "1003" in str(e) or "429" in str(e) or "rate limit" in err_str
            is_temp_error = "timeout" in err_str or "connection" in err_str or "1015" in str(e)

            if attempt < max_retries - 1 and (is_rate_limit or is_temp_error):
                delay = base_delay * (2 ** attempt)
                if is_rate_limit:
                    delay = max(delay, 5.0)
                log(f"API retry {attempt+1}/{max_retries} in {delay:.1f}s: {e}", "warning")
                time.sleep(delay)
            else:
                log(f"API call failed after {attempt+1} attempts: {e}", "error")
                raise
    return None

# ════════════════════════════════════════════════════
#  BALANCE & DAILY LOSS
# ════════════════════════════════════════════════════
def get_balance() -> float:
    """Ambil balance USDT futures yang tersedia."""
    try:
        info = api_call_with_retry(client.futures_account_balance)
        for asset in info:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
    except Exception as e:
        log(f"get_balance error: {e}", "error")
    return 0.0

def get_total_balance() -> float:
    """Total balance (termasuk unrealized PnL)."""
    try:
        info = api_call_with_retry(client.futures_account)
        return float(info.get("totalMarginBalance", 0))
    except Exception as e:
        log(f"get_total_balance error: {e}", "error")
    return 0.0

def check_daily_reset():
    """Reset daily tracking setiap hari (UTC)."""
    global _daily_start_balance, _daily_pnl, _daily_loss_hit, _daily_reset_day, _last_summary_hour
    today = datetime.now(timezone.utc).day
    if today != _daily_reset_day:
        bal = get_total_balance()
        _daily_start_balance = bal
        _daily_pnl           = 0.0
        _daily_loss_hit      = False
        _daily_reset_day     = today
        _last_summary_hour   = -1
        log(f"📅 Daily reset — Balance awal: {bal:.2f} USDT")
        send_telegram(
            f"📅 <b>Daily Reset</b>\n"
            f"💼 Balance awal: <code>{bal:.2f} USDT</code>\n"
            f"🎯 Daily loss limit: -{DAILY_LOSS_LIMIT_PCT*100:.0f}%\n"
            f"🤖 Bot siap trading!"
        )

def update_daily_pnl(pnl: float):
    """Update P&L harian dan cek apakah limit tercapai."""
    global _daily_pnl, _daily_loss_hit
    _daily_pnl += pnl
    if _daily_start_balance > 0:
        loss_pct = _daily_pnl / _daily_start_balance * 100
        if not _daily_loss_hit and loss_pct <= -DAILY_LOSS_LIMIT_PCT * 100:
            _daily_loss_hit = True
            balance = get_total_balance()
            log(f"🚨 DAILY LOSS LIMIT HIT! {loss_pct:.2f}% — stop trading hari ini", "error")
            tg_daily_limit_hit(balance, loss_pct)

def is_daily_limit_hit() -> bool:
    return _daily_loss_hit

def calc_order_usdt(balance: float) -> float:
    """
    Hitung ukuran order berdasarkan balance aktual.
    Risk 1% balance = ukuran order (dalam USDT, tanpa leverage).
    Clamp antara MIN dan MAX.
    """
    order = balance * RISK_PER_TRADE_PCT
    order = max(MIN_ORDER_USDT, min(MAX_ORDER_USDT, order))
    return order

# ════════════════════════════════════════════════════
#  TOP 100 COINS (auto-fetch dari Binance Futures)
# ════════════════════════════════════════════════════
def fetch_top_symbols(max_symbols=100) -> list:
    """
    Fetch symbol yang aktif di Binance Futures, filter meme/micro-cap,
    sort by volume 24h, ambil top N.
    """
    try:
        # Ambil semua ticker 24h
        tickers = api_call_with_retry(client.futures_ticker)
        if not tickers:
            raise Exception("Empty ticker response")

        # Filter: hanya USDT pairs, exclude blacklist
        usdt_tickers = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT", "")
            # Skip kalau ada blacklist keyword
            if any(kw in base.upper() for kw in BLACKLIST_KEYWORDS):
                continue
            # Skip pair leverage token (UP/DOWN/BULL/BEAR)
            if any(suffix in base for suffix in ["UP", "DOWN", "BULL", "BEAR", "3L", "3S"]):
                continue
            try:
                vol_usdt = float(t["quoteVolume"])  # volume dalam USDT 24h
                usdt_tickers.append((sym, vol_usdt))
            except:
                continue

        # Sort by volume (terbesar dulu)
        usdt_tickers.sort(key=lambda x: x[1], reverse=True)

        # Ambil top N
        top_symbols = [sym for sym, vol in usdt_tickers[:max_symbols]]
        log(f"✅ Top {len(top_symbols)} symbols fetched (dari {len(tickers)} total)")
        return top_symbols

    except Exception as e:
        log(f"fetch_top_symbols error: {e} — pakai fallback list", "error")
        # Fallback ke list manual kalau API gagal
        return FALLBACK_SYMBOLS

# Fallback symbols kalau fetch gagal
FALLBACK_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","ATOMUSDT","UNIUSDT","ETCUSDT","NEARUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT",
    "TIAUSDT","AAVEUSDT","RUNEUSDT","FILUSDT","JUPUSDT",
    "SEIUSDT","WLDUSDT","STXUSDT","FTMUSDT","ALGOUSDT",
    "SANDUSDT","MANAUSDT","GALAUSDT","CHZUSDT","APEUSDT",
    "GMXUSDT","DYDXUSDT","RNDRUSDT","FETUSDT","AGIXUSDT",
]

# ════════════════════════════════════════════════════
#  UTILS
# ════════════════════════════════════════════════════
def get_sym_info(symbol):
    if symbol in _sym_info:
        return _sym_info[symbol]
    try:
        info = api_call_with_retry(client.futures_exchange_info)
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        _sym_info[symbol] = {
                            "step":   float(f["stepSize"]),
                            "minQty": float(f["minQty"])
                        }
                        return _sym_info[symbol]
    except Exception as e:
        log(f"get_sym_info({symbol}) error: {e}", "warning")
    return {"step": 1.0, "minQty": 1.0}

def round_step(qty, step):
    p = max(0, int(round(-math.log(step, 10), 0))) if step < 1 else 0
    return round(math.floor(qty / step) * step, p)

def calc_qty(symbol, price, balance):
    """
    Hitung qty berdasarkan balance aktual.
    Risk 1% balance, leverage 10x.
    """
    order_usdt = calc_order_usdt(balance)
    info       = get_sym_info(symbol)
    qty        = (order_usdt * LEVERAGE) / price
    qty        = round_step(qty, info["step"])
    qty        = max(qty, info["minQty"])
    return qty

def set_leverage(symbol):
    try:
        api_call_with_retry(
            client.futures_change_leverage,
            symbol=symbol, leverage=LEVERAGE
        )
    except Exception as e:
        log(f"set_leverage({symbol}) error: {e}", "warning")

def get_price(symbol) -> float:
    try:
        result = api_call_with_retry(
            client.futures_symbol_ticker, symbol=symbol
        )
        return float(result["price"])
    except:
        return 0.0

def validate_symbols(symbols: list) -> list:
    try:
        info  = api_call_with_retry(client.futures_exchange_info)
        valid = {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING"}
        result = list(dict.fromkeys([s for s in symbols if s in valid]))
        log(f"✅ {len(result)} symbols valid")
        return result
    except:
        return list(dict.fromkeys(symbols))

def get_exchange_amt(symbol, retries=3):
    for attempt in range(retries):
        try:
            positions = api_call_with_retry(
                client.futures_position_information, symbol=symbol
            )
            for p in positions:
                amt = float(p["positionAmt"])
                if amt != 0:
                    return amt
            return 0
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                log(f"get_exchange_amt({symbol}) gagal: {e}", "error")
                return None

def is_active_session():
    utc_hour = datetime.now(timezone.utc).hour
    for name, (start, end) in ACTIVE_SESSIONS.items():
        if start <= utc_hour < end:
            return True, name
    return False, "OFF"

def get_session_score_penalty():
    active, _ = is_active_session()
    return 0 if active else OFF_SESSION_PENALTY

# ════════════════════════════════════════════════════
#  OHLCV dengan retry
# ════════════════════════════════════════════════════
def get_ohlcv(symbol, interval, limit=200):
    try:
        klines = api_call_with_retry(
            client.futures_klines,
            symbol=symbol, interval=interval, limit=limit,
            max_retries=3, base_delay=0.5
        )
        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "ct","qv","trades","tbbase","tbquote","ignore"])
        for c in ["open","high","low","close","volume","tbbase","tbquote"]:
            df[c] = df[c].astype(float)
        df["time"] = pd.to_numeric(df["time"])
        return df
    except Exception as e:
        log(f"get_ohlcv({symbol},{interval}) error: {e}", "warning")
        return None

# ════════════════════════════════════════════════════
#  S/R LEVELS (diperbaiki: window 4, ada volume filter)
# ════════════════════════════════════════════════════
def get_sr_levels(symbol, lookback=30):
    """
    v11: Window 4 candle (lebih stabil dari v10 window 2).
    Tambah volume confirmation untuk level yang lebih signifikan.
    """
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_4HOUR, lookback + 10)
    if df is None or len(df) < 15:
        return {"resistance": [], "support": []}

    highs = df["high"].values
    lows  = df["low"].values
    vols  = df["volume"].values
    vol_mean = vols.mean()

    resistance = []
    support    = []
    WINDOW = 4  # v11: was 2

    for i in range(WINDOW, len(highs) - WINDOW):
        # Volume confirmation: level lebih signifikan kalau terbentuk saat volume tinggi
        vol_ok = vols[i] >= vol_mean * 0.8

        if all(highs[i] > highs[i-j] for j in range(1, WINDOW+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, WINDOW+1)) and vol_ok:
            resistance.append(highs[i])

        if all(lows[i] < lows[i-j] for j in range(1, WINDOW+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, WINDOW+1)) and vol_ok:
            support.append(lows[i])

    return {
        "resistance": sorted(resistance, reverse=True)[:4],
        "support":    sorted(support)[:4]
    }

def check_sr_clear(symbol, price, direction):
    sr = get_sr_levels(symbol)
    if direction == "LONG":
        nearby = [r for r in sr["resistance"] if r > price]
        if nearby:
            nearest = min(nearby)
            gap_pct = (nearest - price) / price
            if gap_pct < SR_BUFFER:
                return False, f"Resistance {nearest:.4f} ({gap_pct*100:.2f}% away)"
    elif direction == "SHORT":
        nearby = [s for s in sr["support"] if s < price]
        if nearby:
            nearest = max(nearby)
            gap_pct = (price - nearest) / price
            if gap_pct < SR_BUFFER:
                return False, f"Support {nearest:.4f} ({gap_pct*100:.2f}% away)"
    return True, ""

# ════════════════════════════════════════════════════
#  5m CONFIRMATION (v11: gagal fetch = False, bukan skip)
# ════════════════════════════════════════════════════
def get_5m_confirmation(symbol, direction):
    """
    v11 FIX: Kalau data gagal diambil, return False (bukan True).
    v10 bug: return True kalau fetch error → lolos tanpa konfirmasi.
    """
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_5MINUTE, 20)

    # v11 FIX: gagal fetch → return False
    if df is None or len(df) < 15:
        log(f"[{symbol}] 5m data unavailable — block entry", "warning")
        return False, "5m data unavailable — blocked"

    df = df.iloc[:-1].copy()
    c     = df["close"]
    ema9  = ta.trend.EMAIndicator(c, 9).ema_indicator()
    last  = df.iloc[-1]
    last3 = df.tail(3)

    up_candles   = sum(1 for _, r in last3.iterrows() if r["close"] > r["open"])
    down_candles = sum(1 for _, r in last3.iterrows() if r["close"] < r["open"])

    if direction == "LONG":
        price_above_ema = last["close"] > ema9.iloc[-1]
        momentum_ok     = up_candles >= 2
        if price_above_ema and momentum_ok:
            return True, f"5m✅ ema↑ {up_candles}/3 green"
        elif price_above_ema or momentum_ok:
            return True, f"5m✓ partial"
        return False, f"5m❌ {up_candles}/3 green"
    else:
        price_below_ema = last["close"] < ema9.iloc[-1]
        momentum_ok     = down_candles >= 2
        if price_below_ema and momentum_ok:
            return True, f"5m✅ ema↓ {down_candles}/3 red"
        elif price_below_ema or momentum_ok:
            return True, f"5m✓ partial"
        return False, f"5m❌ {down_candles}/3 red"

# ════════════════════════════════════════════════════
#  REVERSAL DETECTOR
# ════════════════════════════════════════════════════
def detect_reversal(df, direction):
    last = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng  = last["high"] - last["low"]
    if rng == 0:
        return False, 0, ""

    upper_wick  = last["high"] - max(last["close"], last["open"])
    lower_wick  = min(last["close"], last["open"]) - last["low"]
    body_ratio  = body / rng

    if direction == "LONG" and body_ratio < 0.4 and lower_wick > body * 2:
        return True, 6, f"hammer({lower_wick/rng*100:.0f}% wick)"
    if direction == "SHORT" and body_ratio < 0.4 and upper_wick > body * 2:
        return True, 6, f"shooting_star({upper_wick/rng*100:.0f}% wick)"
    return False, 0, ""

# ════════════════════════════════════════════════════
#  MOMENTUM SCORE
# ════════════════════════════════════════════════════
def get_momentum_score(df, direction):
    recent = df.tail(MOMENTUM_CANDLES + 1).iloc[:-1]
    if len(recent) < MOMENTUM_CANDLES:
        return 0, ""

    if direction == "LONG":
        count    = sum(1 for _, r in recent.iterrows() if r["close"] > r["open"])
        closes   = recent["close"].values
        up_seq   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        if count >= 4 and up_seq >= 3:
            return 8, f"momentum {count}/5 up"
        elif count >= 3:
            return 4, f"momentum {count}/5 up"
    else:
        count    = sum(1 for _, r in recent.iterrows() if r["close"] < r["open"])
        closes   = recent["close"].values
        dn_seq   = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        if count >= 4 and dn_seq >= 3:
            return 8, f"momentum {count}/5 down"
        elif count >= 3:
            return 4, f"momentum {count}/5 down"

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
    if price > ema9 > ema21 > ema50 and chg > 0:    return "BULL"
    elif price < ema9 < ema21 < ema50 and chg < 0:  return "BEAR"
    elif price > ema21 and chg > -0.3:               return "MILD_BULL"
    elif price < ema21 and chg < 0.3:                return "MILD_BEAR"
    return "SIDEWAYS"

def refresh_macro(symbols_sample=None):
    now = time.time()

    # Fear & Greed
    if now - _macro["last_fng"] > 300:
        try:
            d = requests.get(
                "https://api.alternative.me/fng/?limit=1", timeout=6
            ).json()["data"][0]
            _macro["fng"]       = int(d["value"])
            _macro["fng_label"] = d["value_classification"]
            _macro["last_fng"]  = now
        except Exception as e:
            log(f"F&G refresh error: {e}", "warning")

    # USDT Dominance
    if now - _macro["last_dom"] > 300:
        try:
            d = requests.get(
                "https://api.coingecko.com/api/v3/global", timeout=10
            ).json()
            _macro["usdt_prev"]      = _macro["usdt_d"]
            _macro["usdt_d"]         = round(d["data"]["market_cap_percentage"].get("usdt", 5), 2)
            _macro["global_mcap_chg"]= round(d["data"].get("market_cap_change_percentage_24h_usd", 0), 2)
            _macro["last_dom"]       = now
        except Exception as e:
            log(f"USDT.D refresh error: {e}", "warning")

    # News Sentiment (v11: robust fallback)
    if now - _macro["last_news"] > 90:
        news_ok = False

        # Coba CryptoPanic dengan API key (kalau ada)
        if CRYPTOPANIC_KEY:
            try:
                url  = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_KEY}&public=true&currencies=BTC"
                data = requests.get(url, timeout=7).json()
                news_ok = _parse_news(data)
                if news_ok:
                    _macro["last_news"] = now
            except Exception as e:
                log(f"CryptoPanic error: {e}", "warning")

        # Fallback: CryptoPanic public (tanpa key, lebih terbatas)
        if not news_ok:
            try:
                url  = "https://cryptopanic.com/api/v1/posts/?public=true&currencies=BTC"
                data = requests.get(url, timeout=7).json()
                news_ok = _parse_news(data)
                if news_ok:
                    _macro["last_news"] = now
            except:
                pass

        # Kalau semua gagal, set ke neutral (bukan tetap nilai lama)
        if not news_ok and now - _macro["last_news"] > 600:
            _macro["news"]          = "neutral"
            _macro["news_strength"] = 0
            log("News unavailable — set to neutral", "warning")

    # BTC Multi-TF
    if now - _macro["last_btc"] > 45:
        try:
            df_15m = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_15MINUTE, 60)
            df_1h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_1HOUR, 60)
            df_4h  = get_ohlcv("BTCUSDT", Client.KLINE_INTERVAL_4HOUR, 60)
            _macro["btc_trend_15m"] = _calc_btc_trend(df_15m)
            _macro["btc_trend_1h"]  = _calc_btc_trend(df_1h)
            _macro["btc_trend_4h"]  = _calc_btc_trend(df_4h)
            _macro["last_btc"]      = now
        except Exception as e:
            log(f"BTC trend error: {e}", "warning")

    # Market Breadth
    if now - _macro["last_breadth"] > 300:
        try:
            sample  = (symbols_sample or FALLBACK_SYMBOLS)[:20]
            bullish = 0
            for sym in sample:
                df = get_ohlcv(sym, Client.KLINE_INTERVAL_15MINUTE, 10)
                if df is not None and len(df) >= 5:
                    c    = df["close"]
                    ema9 = ta.trend.EMAIndicator(c, 9).ema_indicator().iloc[-1]
                    if c.iloc[-1] > ema9 and df["close"].iloc[-1] > df["open"].iloc[-1]:
                        bullish += 1
            _macro["market_breadth"] = bullish / len(sample)
            _macro["last_breadth"]   = now
        except Exception as e:
            log(f"Breadth error: {e}", "warning")

def _parse_news(data) -> bool:
    """Parse CryptoPanic response. Return True kalau berhasil."""
    try:
        neg_kw_strong = ["crash","hack","ban","fraud","collapse","seized","scam","exploit"]
        neg_kw_mild   = ["bear","fear","lawsuit","dump","warning","plunge","fud","sell-off","decline"]
        pos_kw_strong = ["institutional","ath","approved","record","bullish","rally","surge"]
        pos_kw_mild   = ["adoption","breakout","buy","launched","partnership","soar"]
        neg = pos = 0
        hl  = []
        for post in data.get("results", [])[:10]:
            t  = post.get("title", "")
            tl = t.lower()
            if any(w in tl for w in neg_kw_strong):  neg += 2; hl.append(f"🔴🔴 {t[:55]}")
            elif any(w in tl for w in neg_kw_mild):   neg += 1; hl.append(f"🔴 {t[:55]}")
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
        return True
    except:
        return False

# ════════════════════════════════════════════════════
#  ADAPTIVE SCORE THRESHOLD
# ════════════════════════════════════════════════════
def get_adaptive_min_score(direction):
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
        min_score -= SCORE_BONUS_BTC_ALIGN
        bonuses.append(f"BTC3TF-{SCORE_BONUS_BTC_ALIGN}")
    elif btc_align >= 2:
        min_score -= SCORE_BONUS_BTC_ALIGN // 2
        bonuses.append(f"BTC2TF-{SCORE_BONUS_BTC_ALIGN//2}")

    active, session = is_active_session()
    if session == "OVERLAP":
        min_score -= 4; bonuses.append("OVERLAP-4")
    elif active:
        min_score -= 2; bonuses.append(f"{session}-2")
    else:
        min_score += OFF_SESSION_PENALTY; bonuses.append(f"OFF+{OFF_SESSION_PENALTY}")

    fng = _macro["fng"]
    if direction == "LONG" and fng < FNG_FEAR_ZONE:
        min_score += 5; bonuses.append("FNG_FEAR+5")
    elif direction == "SHORT" and fng < FNG_FEAR_ZONE:
        min_score -= 4; bonuses.append("FNG_FEAR_SHORT-4")
    elif direction == "LONG" and fng > FNG_GREED_ZONE:
        min_score -= 3; bonuses.append("FNG_GREED_LONG-3")

    min_score = max(min_score, 38)
    return min_score, bonuses

def btc_multi_tf_ok_for(direction):
    t15 = _macro["btc_trend_15m"]
    t1h = _macro["btc_trend_1h"]
    t4h = _macro["btc_trend_4h"]

    if direction == "LONG":
        if t4h in BEAR_TRENDS and t1h in BEAR_TRENDS:
            return False, f"BTC 4H={t4h} + 1H={t1h} bearish"
        if sum(1 for t in [t15, t1h, t4h] if t in BEAR_TRENDS) >= 3:
            return False, f"BTC 3TF bearish"
    elif direction == "SHORT":
        if t4h in BULL_TRENDS and t1h in BULL_TRENDS:
            return False, f"BTC 4H={t4h} + 1H={t1h} bullish"
        if sum(1 for t in [t15, t1h, t4h] if t in BULL_TRENDS) >= 3:
            return False, f"BTC 3TF bullish"

    return True, ""

# ════════════════════════════════════════════════════
#  REGIME
# ════════════════════════════════════════════════════
def get_regime(symbol):
    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_1HOUR, 60)
    if df is None or len(df) < 55:
        return "RANGE"
    c     = df["close"]
    ema20 = ta.trend.EMAIndicator(c, 20).ema_indicator().iloc[-1]
    ema50 = ta.trend.EMAIndicator(c, 50).ema_indicator().iloc[-1]
    price = c.iloc[-1]
    if ema20 > ema50 and price > ema50: return "BULL"
    if ema20 < ema50 and price < ema50: return "BEAR"
    return "RANGE"

# ════════════════════════════════════════════════════
#  TECHNICAL ANALYSIS
# ════════════════════════════════════════════════════
def run_ta(df):
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    df["rsi"]       = ta.momentum.RSIIndicator(c, 14).rsi()
    df["rsi_fast"]  = ta.momentum.RSIIndicator(c, 7).rsi()
    macd            = ta.trend.MACD(c)
    df["macd"]      = macd.macd()
    df["macd_sig"]  = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    df["ema9"]      = ta.trend.EMAIndicator(c, 9).ema_indicator()
    df["ema21"]     = ta.trend.EMAIndicator(c, 21).ema_indicator()
    df["ema50"]     = ta.trend.EMAIndicator(c, 50).ema_indicator()
    df["ema200"]    = ta.trend.EMAIndicator(c, 200).ema_indicator()
    bb              = ta.volatility.BollingerBands(c, 20, 2)
    df["bb_hi"]     = bb.bollinger_hband()
    df["bb_lo"]     = bb.bollinger_lband()
    df["bb_mid"]    = bb.bollinger_mavg()
    df["bb_width"]  = (df["bb_hi"] - df["bb_lo"]) / df["bb_mid"]
    stoch           = ta.momentum.StochasticOscillator(h, l, c, 14, 3)
    df["stk"]       = stoch.stoch()
    df["std"]       = stoch.stoch_signal()
    df["atr"]       = ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()
    df["vol_ma"]    = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_ma"].replace(0, 1)
    df["buy_ratio"] = df["tbbase"] / df["volume"].replace(0, 1)
    df["body"]      = abs(df["close"] - df["open"])
    df["range_"]    = df["high"] - df["low"]
    df["body_ratio"]= df["body"] / df["range_"].replace(0, 1)
    return df

def calc_composite_score(df, regime, ob_imb, cum_d, funding):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    W    = SCORE_WEIGHTS
    breakdown     = {}
    long_score    = 0.0
    short_score   = 0.0

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
    if rsi < 40:
        pts = W["rsi"] if rsi < 30 else W["rsi"] * 0.6
        long_score += pts; breakdown["rsi"] = f"+{pts:.1f}L(rsi:{rsi:.0f})"
    elif rsi > 60:
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
        if e9 > e21:   long_score  += W["ema_stack"] * 0.5
        elif e9 < e21: short_score += W["ema_stack"] * 0.5
        breakdown["ema"] = "partial"

    # 4. Volume + taker ratio
    vr = last["vol_ratio"]
    br = last["buy_ratio"]
    if vr >= MIN_VOLUME_SPIKE:
        if last["close"] > last["open"] and br > 0.52:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 2.0)
            long_score += pts; breakdown["vol"] = f"+{pts:.1f}L({vr:.1f}x)"
        elif last["close"] < last["open"] and br < 0.48:
            pts = W["volume"] * min(vr / MIN_VOLUME_SPIKE, 2.0)
            short_score += pts; breakdown["vol"] = f"+{pts:.1f}S({vr:.1f}x)"
        else:
            breakdown["vol"] = f"spike({vr:.1f}x) ambiguous"
    else:
        if vr >= 0.9 and last["close"] > last["open"] and br > 0.54:
            long_score += W["volume"] * 0.4
            breakdown["vol"] = f"+{W['volume']*0.4:.1f}L(trend)"
        elif vr >= 0.9 and last["close"] < last["open"] and br < 0.46:
            short_score += W["volume"] * 0.4
            breakdown["vol"] = f"+{W['volume']*0.4:.1f}S(trend)"
        else:
            breakdown["vol"] = f"weak({vr:.1f}x)"

    # 5. OB Imbalance (bobot dikurangi 12→6, kurang reliable)
    if ob_imb > 0.12:
        pts = W["ob_imbalance"] * min(ob_imb / 0.12, 1.5)
        long_score += pts; breakdown["ob"] = f"+{pts:.1f}L({ob_imb:+.2f})"
    elif ob_imb < -0.12:
        pts = W["ob_imbalance"] * min(abs(ob_imb) / 0.12, 1.5)
        short_score += pts; breakdown["ob"] = f"+{pts:.1f}S({ob_imb:+.2f})"
    else:
        breakdown["ob"] = f"neutral({ob_imb:+.2f})"

    # 6. Cumulative Delta (dinaikkan bobot 10→12, lebih reliable dari OB)
    if cum_d > 0.10:
        long_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}L"
    elif cum_d < -0.10:
        short_score += W["cum_delta"]; breakdown["delta"] = f"+{W['cum_delta']}S"
    else:
        breakdown["delta"] = "0"

    # 7. Stochastic
    k, d    = last["stk"], last["std"]
    pk, pd_ = prev["stk"], prev["std"]
    if k < 30 and k > d and pk <= pd_:
        long_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}L"
    elif k > 70 and k < d and pk >= pd_:
        short_score += W["stoch"]; breakdown["stoch"] = f"+{W['stoch']}S"
    else:
        breakdown["stoch"] = "0"

    # 8. Bollinger Band
    price = last["close"]
    if price <= last["bb_lo"] * 1.005:
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

    if long_pct > short_pct + 8:
        return "LONG", long_pct, breakdown
    if short_pct > long_pct + 8:
        return "SHORT", short_pct, breakdown
    return "NONE", max(long_pct, short_pct), breakdown

def check_volume_or_momentum(df, direction):
    recent = df.iloc[-4:-1]
    for _, row in recent.iterrows():
        vr = row["vol_ratio"]
        br = row["buy_ratio"]
        if vr >= MIN_VOLUME_SPIKE:
            if direction == "LONG" and row["close"] > row["open"] and br > 0.52:
                return True, f"vol_spike {vr:.1f}x"
            if direction == "SHORT" and row["close"] < row["open"] and br < 0.48:
                return True, f"vol_spike {vr:.1f}x"
    mom_bonus, mom_desc = get_momentum_score(df, direction)
    if mom_bonus >= 4:
        return True, f"momentum: {mom_desc}"
    return False, "no vol spike & weak momentum"

def get_ob_imbalance(symbol):
    try:
        ob    = api_call_with_retry(client.futures_order_book, symbol=symbol, limit=50)
        bids  = sum(float(b[1]) for b in ob["bids"])
        asks  = sum(float(a[1]) for a in ob["asks"])
        total = bids + asks
        return round((bids - asks) / total, 3) if total else 0.0
    except:
        return 0.0

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
        data = api_call_with_retry(
            client.futures_funding_rate, symbol=symbol, limit=1
        )
        return round(float(data[0]["fundingRate"]) * 100, 4)
    except:
        return 0.0

# ════════════════════════════════════════════════════
#  COOLDOWN
# ════════════════════════════════════════════════════
def is_symbol_in_cooldown(symbol):
    if symbol not in _symbol_cooldown:
        return False
    elapsed = time.time() - _symbol_cooldown[symbol]
    if elapsed > SYMBOL_COOLDOWN_SECS:
        del _symbol_cooldown[symbol]
        return False
    return True

def get_symbol_cooldown_remaining(symbol):
    if symbol not in _symbol_cooldown: return 0
    return max(0, SYMBOL_COOLDOWN_SECS - (time.time() - _symbol_cooldown[symbol]))

def set_symbol_cooldown(symbol):
    _symbol_cooldown[symbol] = time.time()
    log(f"[{symbol}] Symbol cooldown {SYMBOL_COOLDOWN_SECS}s")

def check_global_cooldown_recover():
    btc_ok     = _macro["btc_trend_15m"] in COOLDOWN_BTC_RECOVER
    breadth_ok = _macro["market_breadth"] >= COOLDOWN_BREADTH_MIN
    return btc_ok or breadth_ok

def is_cooldown_active():
    global _in_cooldown
    if not _in_cooldown: return False
    if check_global_cooldown_recover():
        _in_cooldown = False
        log("✅ Global cooldown selesai!")
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
def should_enter(symbol, df, balance):
    info = {}

    # ── 0. Daily loss limit ───────────────────────────
    if is_daily_limit_hit():
        return None, 0, 0, 0, {"skip": "🚨 Daily loss limit hit"}

    # ── 1. Symbol cooldown ────────────────────────────
    if is_symbol_in_cooldown(symbol):
        rem = get_symbol_cooldown_remaining(symbol)
        return None, 0, 0, 0, {"skip": f"⏳ Symbol cooldown {rem:.0f}s"}

    # ── 2. Global cooldown ────────────────────────────
    if is_cooldown_active():
        return None, 0, 0, 0, {"skip": f"🧊 Global cooldown ({cooldown_reason()})"}

    # ── 3. Balance check ──────────────────────────────
    if balance < MIN_ORDER_USDT * 2:
        return None, 0, 0, 0, {"skip": f"Balance terlalu kecil ({balance:.1f} USDT)"}

    # ── 4. Macro hard blocks ──────────────────────────
    fng     = _macro["fng"]
    news    = _macro["news"]
    usdt_up = _macro["usdt_d"] > _macro["usdt_prev"] + USDT_RISK_OFF_DELTA

    if fng < MIN_FNG_ANY:
        return None, 0, 0, 0, {"skip": f"F&G ekstrem ({fng})"}
    if news == "strong_negative":
        return None, 0, 0, 0, {"skip": "News strong_negative"}
    if usdt_up:
        return None, 0, 0, 0, {"skip": f"USDT.D risk-off"}

    # ── 5. BTC info ───────────────────────────────────
    info["btc_15m"] = _macro["btc_trend_15m"]
    info["btc_1h"]  = _macro["btc_trend_1h"]
    info["btc_4h"]  = _macro["btc_trend_4h"]
    info["breadth"] = f"{_macro['market_breadth']*100:.0f}%"

    # ── 6. Regime ─────────────────────────────────────
    regime = get_regime(symbol)
    info["regime"] = regime

    # ── 7. Candle timing ──────────────────────────────
    prev_candle_time = int(df["time"].iloc[-2])
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 60:
        return None, 0, 0, 0, {"skip": "Data tidak cukup"}
    if _last_candle.get(symbol) == prev_candle_time:
        return None, 0, 0, 0, {"skip": "Sudah dianalisa"}

    # ── 8. TA + Score ─────────────────────────────────
    df_closed = run_ta(df_closed)
    ob_imb    = get_ob_imbalance(symbol)
    cum_d     = get_cum_delta(df_closed)
    funding   = get_funding(symbol)
    ta_dir, score, breakdown = calc_composite_score(df_closed, regime, ob_imb, cum_d, funding)
    info["score"]     = f"{score:.1f}/100"
    info["breakdown"] = breakdown

    if ta_dir == "NONE":
        return None, 0, 0, 0, {"skip": f"Score tidak meyakinkan ({score:.1f})"}

    # ── 9. Adaptive min score ─────────────────────────
    min_score, score_bonuses = get_adaptive_min_score(ta_dir)
    info["min_score"] = min_score
    info["score_ctx"] = score_bonuses
    if score < min_score:
        return None, 0, 0, 0, {"skip": f"Score {score:.1f} < min {min_score}"}

    # ── 10. F&G directional ───────────────────────────
    if ta_dir == "LONG" and fng > MAX_FNG_LONG:
        return None, 0, 0, 0, {"skip": f"F&G euphoria ({fng})"}
    if ta_dir == "LONG" and fng < MIN_FNG_ANY + 10 and score < min_score + 10:
        return None, 0, 0, 0, {"skip": f"Fear F&G={fng}, score kurang kuat"}

    # ── 11. BTC Multi-TF ──────────────────────────────
    btc_ok, btc_reason = btc_multi_tf_ok_for(ta_dir)
    if not btc_ok:
        return None, 0, 0, 0, {"skip": btc_reason}

    # ── 12. Breadth ───────────────────────────────────
    breadth = _macro["market_breadth"]
    if ta_dir == "LONG" and breadth < MIN_MARKET_BREADTH:
        return None, 0, 0, 0, {"skip": f"Breadth {breadth*100:.0f}% < {MIN_MARKET_BREADTH*100:.0f}%"}
    if ta_dir == "SHORT" and breadth > 0.70:
        return None, 0, 0, 0, {"skip": f"Breadth {breadth*100:.0f}% terlalu tinggi untuk SHORT"}

    # ── 13. Regime vs direction ───────────────────────
    if ta_dir == "LONG" and regime == "BEAR" and score < min_score + 15:
        return None, 0, 0, 0, {"skip": "BEAR regime, counter-trend LONG score kurang"}
    if ta_dir == "SHORT" and regime == "BULL" and score < min_score + 15:
        return None, 0, 0, 0, {"skip": "BULL regime, counter-trend SHORT score kurang"}

    # ── 14. Volume/Momentum ───────────────────────────
    vol_ok, vol_info = check_volume_or_momentum(df_closed, ta_dir)
    if not vol_ok:
        return None, 0, 0, 0, {"skip": f"Vol/mom lemah: {vol_info}"}
    info["vol_momentum"] = vol_info

    # ── 15. Reversal detector ─────────────────────────
    rev_detected, rev_bonus, rev_desc = detect_reversal(df_closed, ta_dir)
    info["reversal"] = rev_desc if rev_detected else "-"

    # ── 16. S/R Check ─────────────────────────────────
    sr_ok, sr_reason = check_sr_clear(symbol, df_closed["close"].iloc[-1], ta_dir)
    if not sr_ok:
        return None, 0, 0, 0, {"skip": f"S/R: {sr_reason}"}
    info["sr"] = "clear"

    # ── 17. 5m Confirmation ───────────────────────────
    m5_ok, m5_info = get_5m_confirmation(symbol, ta_dir)
    info["5m"] = m5_info
    if not m5_ok and score < min_score + 8:
        return None, 0, 0, 0, {"skip": f"5m contra: {m5_info}"}

    # ── 18. Whale filter ──────────────────────────────
    whale_dir, whale_ratio = detect_whale(df_closed)
    info["whale"] = f"{whale_dir}({whale_ratio:.1f}x)"
    if ta_dir == "LONG" and whale_dir == "sell_whale":
        return None, 0, 0, 0, {"skip": "Whale sell aktif"}
    if ta_dir == "SHORT" and whale_dir == "buy_whale":
        return None, 0, 0, 0, {"skip": "Whale buy aktif"}

    # ── 19. Funding extreme ───────────────────────────
    info["funding"] = funding
    if ta_dir == "LONG" and funding > 0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu positif"}
    if ta_dir == "SHORT" and funding < -0.1:
        return None, 0, 0, 0, {"skip": "Funding terlalu negatif"}

    # ── 20. BB Width ──────────────────────────────────
    bb_width = df_closed["bb_width"].iloc[-1]
    info["bb_width"] = round(bb_width, 4)
    if bb_width < 0.008:
        return None, 0, 0, 0, {"skip": f"BB sempit ({bb_width:.4f})"}

    # ── 21. ATR SL/TP ─────────────────────────────────
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
    if sl_pct > 0.035:
        return None, 0, 0, 0, {"skip": f"ATR terlalu besar (SL={sl_pct*100:.1f}%)"}

    _last_candle[symbol] = prev_candle_time
    save_last_candle()  # v11: persist setelah update

    info["atr_sl_pct"] = f"{sl_pct*100:.2f}%"
    info["ob"]         = ob_imb
    info["ta"]         = ta_dir
    info["score_num"]  = score
    return ta_dir, sl_price, tp1_price, tp2_price, info

# ════════════════════════════════════════════════════
#  TRADE EXECUTION
# ════════════════════════════════════════════════════
def open_trade(symbol, side, sl_price, tp1_price, tp2_price, info):
    try:
        # v11: check balance sebelum entry
        balance = get_balance()
        if balance < MIN_ORDER_USDT * 2:
            log(f"[{symbol}] Balance tidak cukup ({balance:.1f} USDT) — skip", "warning")
            return

        set_leverage(symbol)
        price = get_price(symbol)
        qty   = calc_qty(symbol, price, balance)

        api_call_with_retry(
            client.futures_create_order,
            symbol=symbol,
            side=SIDE_BUY if side == "LONG" else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )

        entry    = get_price(symbol)
        trail_sl = entry * (1 - TRAIL_PCT) if side == "LONG" else entry * (1 + TRAIL_PCT)
        order_val = entry * qty / LEVERAGE  # nilai USDT tanpa leverage

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
            "order_usdt":       order_val,
        }

        sl_pct  = abs(entry - sl_price)  / entry * 100
        tp1_pct = abs(tp1_price - entry) / entry * 100
        tp2_pct = abs(tp2_price - entry) / entry * 100
        score   = info.get("score", "?")
        _, session = is_active_session()
        est_pnl_sl = -(sl_pct / 100) * order_val * LEVERAGE

        log(f"✅ [{symbol}] {side} @{entry:.5f} qty={qty} order≈{order_val:.1f}U")
        log(f"   SL:{sl_price:.5f}(-{sl_pct:.2f}%) TP1:{tp1_price:.5f}(+{tp1_pct:.2f}%) TP2:{tp2_price:.5f}(+{tp2_pct:.2f}%)")
        log(f"   Score:{score} | Session:{session} | BTC:{info.get('btc_15m','?')}/{info.get('btc_1h','?')}/{info.get('btc_4h','?')}")

        # Telegram notif open
        tg_open(symbol, side, entry, sl_price, tp1_price, tp2_price,
                score, session, qty, est_pnl_sl)

    except Exception as e:
        log(f"❌ [{symbol}] Gagal entry: {e}", "error")
        tg_alert(f"Entry GAGAL [{symbol}]: {e}")

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

        api_call_with_retry(
            client.futures_create_order,
            symbol=symbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=close_qty,
            reduceOnly=True
        )

        exit_price = get_price(symbol)
        side       = pos["side"]
        pnl        = (exit_price - pos["entry"]) * close_qty if side == "LONG" \
                     else (pos["entry"] - exit_price) * close_qty
        pct        = pnl / (pos["entry"] * close_qty) * 100

        log(f"🎯 [{symbol}] PARTIAL {reason} @{exit_price:.5f} P&L:{pnl:+.4f}U ({pct:+.2f}%)")

        # Update daily PnL
        update_daily_pnl(pnl)
        trade_log.append({"symbol": symbol, "side": side, "pnl": round(pnl, 4), "reason": f"Partial {reason}"})

        pos["tp1_hit"]         = True
        pos["qty_remain"]      = abs(amt) - close_qty
        pos["be_active"]       = True
        pos["sl"]              = pos["entry"]
        pos["trailing_active"] = True
        pos["peak"]            = exit_price
        pos["trail_sl"]        = exit_price * (1 - TRAIL_PCT) if side == "LONG" \
                                 else exit_price * (1 + TRAIL_PCT)

        balance = get_total_balance()
        tg_close(symbol, side, pos["entry"], exit_price, pnl, pct, f"Partial {reason}", balance)

    except Exception as e:
        log(f"❌ [{symbol}] Gagal partial: {e}", "error")
        pos["tp1_hit"] = True

def close_trade(symbol, reason=""):
    global _consec_loss, _in_cooldown
    try:
        amt = get_exchange_amt(symbol)
        if amt is None:
            log(f"⚠️  [{symbol}] Query gagal, tunda close", "warning")
            return False
        if amt == 0:
            if symbol in open_positions:
                pos   = open_positions[symbol]
                exit_ = get_price(symbol)
                if exit_ > 0:
                    qty_r = pos.get("qty_remain", pos["qty"])
                    pnl   = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                            else (pos["entry"] - exit_) * qty_r
                    update_daily_pnl(pnl)
                    trade_log.append({"symbol": symbol, "side": pos["side"],
                                      "pnl": round(pnl, 4), "reason": "External close"})
                    _update_loss_streak(symbol, pnl)
                open_positions.pop(symbol, None)
            return True

        api_call_with_retry(
            client.futures_create_order,
            symbol=symbol,
            side=SIDE_SELL if amt > 0 else SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=abs(amt),
            reduceOnly=True
        )

        if symbol in open_positions:
            pos   = open_positions[symbol]
            exit_ = get_price(symbol)
            qty_r = pos.get("qty_remain", pos["qty"])
            pnl   = (exit_ - pos["entry"]) * qty_r if pos["side"] == "LONG" \
                    else (pos["entry"] - exit_) * qty_r
            pct   = pnl / (pos["entry"] * qty_r) * 100 if qty_r > 0 else 0
            emoji = "🟢" if pnl >= 0 else "🔴"
            be_tag = " [BE]" if pos.get("be_active") else ""
            log(f"💰 [{symbol}] CLOSED — {reason}{be_tag} | {emoji} {pnl:+.4f}U ({pct:+.2f}%)")

            update_daily_pnl(pnl)
            trade_log.append({"symbol": symbol, "side": pos["side"],
                              "pnl": round(pnl, 4), "reason": reason})
            _update_loss_streak(symbol, pnl)

            balance = get_total_balance()
            tg_close(symbol, pos["side"], pos["entry"], exit_, pnl, pct, reason, balance)

        open_positions.pop(symbol, None)
        return True

    except Exception as e:
        log(f"❌ [{symbol}] Gagal close: {e}", "error")
        return False

def _update_loss_streak(symbol, pnl):
    global _consec_loss, _in_cooldown
    if pnl < 0:
        _consec_loss += 1
        set_symbol_cooldown(symbol)
        if _consec_loss >= GLOBAL_COOLDOWN_LOSS:
            btc_bad     = _macro["btc_trend_15m"] in COOLDOWN_BTC_BAD
            breadth_bad = _macro["market_breadth"] < COOLDOWN_BREADTH_MAX
            if btc_bad and breadth_bad:
                _in_cooldown = True
                log(f"🧊 {GLOBAL_COOLDOWN_LOSS} loss + market buruk → Global Cooldown!", "warning")
                send_telegram(f"🧊 <b>Global Cooldown Aktif</b>\n"
                              f"Loss streak: {_consec_loss}\n"
                              f"BTC: {_macro['btc_trend_15m']} | Breadth: {_macro['market_breadth']*100:.0f}%")
            else:
                log(f"⚡ {_consec_loss} loss tapi market masih oke → symbol cooldown aja")
                _consec_loss = 0
    else:
        _consec_loss = 0
        if _in_cooldown:
            _in_cooldown = False
            log("✅ Win! Global cooldown selesai.")

# ════════════════════════════════════════════════════
#  POSITION MANAGEMENT
# ════════════════════════════════════════════════════
def manage_positions():
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_price(symbol)
        if price == 0:
            log(f"⚠️  [{symbol}] Tidak bisa get price", "warning")
            continue

        entry = pos["entry"]
        side  = pos["side"]

        # Emergency exits
        if _macro["news"] == "strong_negative":
            close_trade(symbol, "🚨 Emergency bad news"); continue
        if side == "LONG" and _macro["btc_trend_1h"] == "BEAR" and _macro["btc_trend_4h"] == "BEAR":
            close_trade(symbol, "⚡ BTC 1H+4H BEAR — emergency exit"); continue
        if side == "SHORT" and _macro["btc_trend_1h"] == "BULL" and _macro["btc_trend_4h"] == "BULL":
            close_trade(symbol, "⚡ BTC 1H+4H BULL — emergency exit"); continue

        if side == "LONG":
            profit_pct = (price - entry) / entry

            if not pos["tp1_hit"] and price >= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 - TRAIL_PCT)
                log(f"🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price > pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 - TRAIL_PCT)

            if pos["tp1_hit"] and price >= pos["tp2"]:
                close_trade(symbol, "✨ TP2"); continue

            if pos["trailing_active"] and price <= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue

            if price <= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason); continue

            pnl_now = (price - entry) * pos.get("qty_remain", pos["qty"])
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            tp_tag  = f" TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f" TP1:{pos['tp1']:.4f}"
            be_tag  = " [BE]" if pos.get("be_active") else ""
            log(f"📌 [{symbol}] LONG @{entry:.4f}→{price:.4f}{be_tag} | {pnl_now:+.3f}U{tsl}{tp_tag}")

        else:  # SHORT
            profit_pct = (entry - price) / entry

            if not pos["tp1_hit"] and price <= pos["tp1"]:
                partial_close(symbol, "TP1"); continue

            if not pos["trailing_active"] and profit_pct >= TRAIL_TRIGGER:
                pos["trailing_active"] = True
                pos["trail_sl"] = price * (1 + TRAIL_PCT)
                log(f"🔄 [{symbol}] Trailing aktif @{price:.5f} (+{profit_pct*100:.2f}%)")

            if pos["trailing_active"] and price < pos["peak"]:
                pos["peak"]     = price
                pos["trail_sl"] = price * (1 + TRAIL_PCT)

            if pos["tp1_hit"] and price <= pos["tp2"]:
                close_trade(symbol, "✨ TP2"); continue

            if pos["trailing_active"] and price >= pos["trail_sl"]:
                close_trade(symbol, "🔄 Trailing Stop"); continue

            if price >= pos["sl"]:
                reason = "🔒 Break-even" if pos.get("be_active") else "🛑 STOP LOSS"
                close_trade(symbol, reason); continue

            pnl_now = (entry - price) * pos.get("qty_remain", pos["qty"])
            tsl     = f" TSL:{pos['trail_sl']:.4f}" if pos["trailing_active"] else ""
            tp_tag  = f" TP2:{pos['tp2']:.4f}" if pos["tp1_hit"] else f" TP1:{pos['tp1']:.4f}"
            be_tag  = " [BE]" if pos.get("be_active") else ""
            log(f"📌 [{symbol}] SHORT @{entry:.4f}→{price:.4f}{be_tag} | {pnl_now:+.3f}U{tsl}{tp_tag}")

# ════════════════════════════════════════════════════
#  DAILY SUMMARY (auto tiap jam 23:00 UTC)
# ════════════════════════════════════════════════════
def maybe_send_daily_summary():
    global _last_summary_hour
    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour == 23 and _last_summary_hour != 23:
        _last_summary_hour = 23
        n       = len(trade_log)
        wins    = sum(1 for t in trade_log if t["pnl"] > 0)
        total   = sum(t["pnl"] for t in trade_log)
        wr      = wins / n * 100 if n else 0
        balance = get_total_balance()
        daily_loss_pct = _daily_pnl / _daily_start_balance * 100 if _daily_start_balance > 0 else 0
        tg_daily_summary(n, total, wr, balance, daily_loss_pct)

# ════════════════════════════════════════════════════
#  SUMMARY PRINT
# ════════════════════════════════════════════════════
def print_summary():
    if not trade_log: return
    total = sum(t["pnl"] for t in trade_log)
    wins  = sum(1 for t in trade_log if t["pnl"] > 0)
    n     = len(trade_log)
    wr    = wins / n * 100 if n else 0
    daily_loss_pct = _daily_pnl / _daily_start_balance * 100 if _daily_start_balance > 0 else 0
    cd_info = f" | 🧊 GlobalCD" if _in_cooldown else ""
    limit_info = " | 🚨 DAILY LIMIT HIT" if _daily_loss_hit else ""
    log(f"📊 {n} trades | WR:{wr:.0f}% W:{wins} L:{n-wins} | P&L:{total:+.4f}U | Daily:{daily_loss_pct:+.1f}%{cd_info}{limit_info}")
    for t in trade_log[-3:]:
        e = "🟢" if t["pnl"] > 0 else "🔴"
        log(f"   {e} {t['symbol']} {t['side']} {t['pnl']:+.4f}U — {t['reason'][:40]}")

# ════════════════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════════════════
def run_bot():
    log("=" * 72)
    log("🤖 Bot v11 — Smart Aggressive Scalping (PRODUCTION READY)")
    log(f"   Risk/trade : {RISK_PER_TRADE_PCT*100:.1f}% balance (max ${MAX_ORDER_USDT} USDT)")
    log(f"   Daily limit: -{DAILY_LOSS_LIMIT_PCT*100:.0f}% dari balance awal hari")
    log(f"   Leverage   : {LEVERAGE}x")
    log(f"   SL/TP      : {ATR_SL_MULT}x ATR SL | TP1:{ATR_TP1_MULT}x | TP2:{ATR_TP2_MULT}x")
    log(f"   Trailing   : aktif setelah +{TRAIL_TRIGGER*100:.1f}%")
    log(f"   Score base : {BASE_MIN_SCORE} (adaptive)")
    log(f"   OB weight  : 6 (was 12 — dikurangi karena kurang reliable)")
    log(f"   S/R window : 4 candle (was 2)")
    log(f"   5m confirm : fail=BLOCK (was fail=OK)")
    log(f"   Telegram   : {'✅ Connected' if TELEGRAM_TOKEN else '❌ Not configured'}")
    log(f"   CryptoPanic: {'✅ API Key' if CRYPTOPANIC_KEY else '⚠️  No key (fallback)'}")
    log("=" * 72)

    # Setup
    load_last_candle()
    check_daily_reset()

    log("⏳ Fetching top 100 symbols...")
    raw_symbols = fetch_top_symbols(max_symbols=100)
    symbols     = validate_symbols(raw_symbols)
    log(f"✅ {len(symbols)} symbols ready")

    # Pre-cache sym_info
    log("⏳ Pre-caching symbol info...")
    for s in symbols[:50]:  # cache 50 dulu (yang paling sering dipake)
        get_sym_info(s)
    time.sleep(1)

    refresh_macro(symbols_sample=symbols[:20])
    balance = get_total_balance()
    active, session = is_active_session()

    log(f"💼 Balance   : {balance:.2f} USDT")
    log(f"📊 F&G       : {_macro['fng']} ({_macro['fng_label']})")
    log(f"📈 BTC trend : 15m={_macro['btc_trend_15m']} 1H={_macro['btc_trend_1h']} 4H={_macro['btc_trend_4h']}")
    log(f"📅 Session   : {session} ({'AKTIF' if active else 'OFF'})")
    log(f"📰 News      : {_macro['news']}")
    log(f"🌍 Breadth   : {_macro['market_breadth']*100:.0f}%")

    send_telegram(
        f"🤖 <b>Bot v11 Mulai!</b>\n"
        f"💼 Balance: <code>{balance:.2f} USDT</code>\n"
        f"🔢 Symbols: {len(symbols)}\n"
        f"📊 F&G: {_macro['fng']} | BTC: {_macro['btc_trend_15m']}\n"
        f"📅 Session: {session}"
    )

    # Re-fetch symbols tiap 6 jam
    symbol_refresh_time = time.time()
    SYMBOL_REFRESH_INTERVAL = 6 * 3600

    cycle = 0
    while True:
        try:
            cycle += 1
            check_daily_reset()
            refresh_macro(symbols_sample=symbols[:20])
            maybe_send_daily_summary()

            # Re-fetch top 100 tiap 6 jam
            if time.time() - symbol_refresh_time > SYMBOL_REFRESH_INTERVAL:
                log("🔄 Refresh top 100 symbols...")
                raw_new = fetch_top_symbols(max_symbols=100)
                symbols = validate_symbols(raw_new)
                symbol_refresh_time = time.time()
                log(f"✅ Symbols updated: {len(symbols)}")

            if is_cooldown_active():
                pass  # cooldown sudah di-handle

            manage_positions()

            active, session = is_active_session()
            balance = get_balance()
            daily_pct = _daily_pnl / _daily_start_balance * 100 if _daily_start_balance > 0 else 0

            log("=" * 72)
            log(f"🔄 #{cycle} {datetime.now().strftime('%H:%M:%S')} | "
                f"F&G:{_macro['fng']} | USDT.D:{_macro['usdt_d']}% | "
                f"News:{_macro['news']} | Bal:{balance:.1f}U")
            log(f"📈 BTC: {_macro['btc_trend_15m']}/{_macro['btc_trend_1h']}/{_macro['btc_trend_4h']} | "
                f"Breadth:{_macro['market_breadth']*100:.0f}% | Session:{session} | DailyP&L:{daily_pct:+.1f}%")

            if _daily_loss_hit:
                log("🚨 DAILY LOSS LIMIT HIT — bot tidak entry baru hari ini")
                log("=" * 72)
                print_summary()
                time.sleep(SCAN_INTERVAL)
                continue

            for h in _macro["headlines"]:
                log(f"  {h}")

            log(f"📂 Posisi({len(open_positions)}): {list(open_positions.keys()) or '-'}")
            log("=" * 72)

            skipped      = 0
            skip_reasons = {}
            candidates   = []

            if len(open_positions) < MAX_POSITIONS and not _in_cooldown and \
               _macro["news"] != "strong_negative":
                for symbol in symbols:
                    if symbol in open_positions: continue
                    df = get_ohlcv(symbol, Client.KLINE_INTERVAL_15MINUTE, 220)
                    if df is None or len(df) < 70: continue

                    side, sl, tp1, tp2, info = should_enter(symbol, df, balance)
                    if side:
                        candidates.append((symbol, side, sl, tp1, tp2, info))
                    else:
                        skipped += 1
                        reason = info.get("skip", "?").split(" ")[0]
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

                if candidates:
                    candidates.sort(key=lambda x: x[5].get("score_num", 0), reverse=True)
                    log(f"🎯 {len(candidates)} setup valid | {skipped} skip")
                    for sym, side, sl, tp1, tp2, info in candidates[:3]:
                        log(f"   ⭐ {sym} {side} | Score:{info.get('score','?')} | "
                            f"Vol:{info.get('vol_momentum','?')} | 5m:{info.get('5m','?')}")
                    for sym, side, sl, tp1, tp2, info in candidates:
                        if len(open_positions) >= MAX_POSITIONS: break
                        open_trade(sym, side, sl, tp1, tp2, info)
                else:
                    log(f"⏳ {skipped} coins di-scan, belum ada setup valid")
                    if skip_reasons:
                        top = sorted(skip_reasons.items(), key=lambda x: -x[1])[:5]
                        log(f"🔍 Skip reasons: {' | '.join(f'{k}:{v}' for k,v in top)}")
            else:
                if _in_cooldown:
                    log(f"🧊 Global Cooldown — {cooldown_reason()}")
                elif _daily_loss_hit:
                    log("🚨 Daily loss limit — tidak ada entry baru")
                else:
                    log("⏸️  Posisi penuh atau kondisi tidak aman")

            print_summary()
            log(f"⏱️  Next scan {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log("\n🛑 Bot dihentikan manual (Ctrl+C)")
            tg_alert("Bot dihentikan manual!")
            break
        except Exception as e:
            log(f"❌ Loop error: {e}\n{traceback.format_exc()}", "error")
            tg_alert(f"Loop error: {str(e)[:200]}")
            time.sleep(30)  # tunggu 30s kalau ada error tak terduga

if __name__ == "__main__":
    run_bot()
