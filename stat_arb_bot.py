"""
Statistical Arbitrage Bot - Binance Demo Mode / Testnet
Pair: BTC/USDT & ETH/USDT

Setup:
1. Ambil API key: https://demo.binance.com/en/my/settings/api-management
2. pip install python-binance pandas numpy python-dotenv
3. Isi .env, jalankan: python stat_arb_bot.py
"""

import os
import math
import time
import numpy as np
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime
from dotenv import load_dotenv

# ─── LOAD ENV ─────────────────────────────────────────────────────────────────

load_dotenv()

MODE = os.getenv("BINANCE_MODE", "demo").lower()

if MODE == "demo":
    API_KEY    = os.getenv("BINANCE_DEMO_API_KEY")
    API_SECRET = os.getenv("BINANCE_DEMO_API_SECRET")
else:
    API_KEY    = os.getenv("BINANCE_TESTNET_API_KEY")
    API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET")

if not API_KEY or not API_SECRET:
    raise ValueError(f"❌ API Key tidak ditemukan untuk mode '{MODE}'!")

# ─── KONFIGURASI ──────────────────────────────────────────────────────────────

SYMBOL_A       = "BTCUSDT"
SYMBOL_B       = "ETHUSDT"
LOOKBACK       = 30
Z_ENTRY        = 2.5
Z_EXIT         = 0.5
TRADE_USDT_PCT = 0.05    # 5% dari saldo USDT per trade
INTERVAL_SEC   = 10
BTC_PRECISION  = 5
ETH_PRECISION  = 3

# ─── INIT CLIENT ──────────────────────────────────────────────────────────────

if MODE == "demo":
    client = Client(API_KEY, API_SECRET)
    client.API_URL = "https://demo-api.binance.com/api"
else:
    client = Client(API_KEY, API_SECRET, testnet=True)

# ─── STATE ────────────────────────────────────────────────────────────────────

position  = None
trade_log = []

# ─── HELPER ───────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_price(symbol):
    return float(client.get_symbol_ticker(symbol=symbol)["price"])

def get_balance(asset):
    info = client.get_asset_balance(asset=asset)
    return float(info["free"]) if info else 0.0

def floor_qty(qty, precision):
    factor = 10 ** precision
    return math.floor(qty * factor) / factor

def print_balances():
    btc  = get_balance("BTC")
    eth  = get_balance("ETH")
    usdt = get_balance("USDT")
    log(f"  💰 BTC: {btc:.6f} | ETH: {eth:.5f} | USDT: {usdt:.2f}")

def place_order(symbol, side, qty):
    try:
        order = client.create_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty
        )
        log(f"  ✅ {side} {qty} {symbol} | orderId: {order['orderId']}")
        return order
    except BinanceAPIException as e:
        log(f"  ❌ Gagal {side} {symbol}: {e.message}")
        return None

def zscore(series):
    s = np.std(series)
    return 0.0 if s == 0 else (series[-1] - np.mean(series)) / s

def calc_entry_qty(price_btc, price_eth):
    """Hitung qty BTC+ETH dari 5% USDT, alokasi 50/50."""
    usdt = get_balance("USDT")
    half = (usdt * TRADE_USDT_PCT) / 2
    return floor_qty(half / price_btc, BTC_PRECISION), floor_qty(half / price_eth, ETH_PRECISION)

def sell_all_btc_eth():
    """
    Exit LONG: jual semua BTC dan ETH yang ada di balance.
    Pakai balance aktual, bukan qty yang disimpan — supaya nggak kena masalah fee.
    """
    btc = floor_qty(get_balance("BTC"), BTC_PRECISION)
    eth = floor_qty(get_balance("ETH"), ETH_PRECISION)

    ok_btc = ok_eth = False

    if btc > 0:
        o = place_order(SYMBOL_A, "SELL", btc)
        ok_btc = o is not None
    else:
        log("  ⚠️  BTC balance 0, skip sell BTC")
        ok_btc = True  # anggap ok kalau memang 0

    if eth > 0:
        o = place_order(SYMBOL_B, "SELL", eth)
        ok_eth = o is not None
    else:
        log("  ⚠️  ETH balance 0, skip sell ETH")
        ok_eth = True

    return ok_btc and ok_eth

def buy_back_btc_eth(qty_btc, qty_eth):
    """Exit SHORT: beli kembali BTC dan ETH pakai USDT."""
    o1 = place_order(SYMBOL_A, "BUY", qty_btc)
    o2 = place_order(SYMBOL_B, "BUY", qty_eth)
    return o1 is not None and o2 is not None

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_bot():
    global position

    # qty SHORT disimpan saat entry, supaya beli balik qty yang sama
    short_qty_btc = 0.0
    short_qty_eth = 0.0

    mode_label = "Demo Mode" if MODE == "demo" else "Testnet"
    log("=" * 60)
    log(f"🤖 Stat Arb Bot — {mode_label} | {SYMBOL_A} vs {SYMBOL_B}")
    log(f"   Z-entry: ±{Z_ENTRY} | Z-exit: ±{Z_EXIT} | Trade: {TRADE_USDT_PCT*100:.0f}% USDT")
    log("=" * 60)
    print_balances()

    hist_a, hist_b = [], []

    while True:
        try:
            price_a = get_price(SYMBOL_A)
            price_b = get_price(SYMBOL_B)
            spread  = price_b / price_a

            hist_a.append(price_a)
            hist_b.append(price_b)
            spreads = [b / a for a, b in zip(hist_a, hist_b)]

            log(f"BTC: ${price_a:,.2f} | ETH: ${price_b:,.2f} | Spread: {spread:.6f}")

            if len(spreads) < LOOKBACK:
                log(f"  ⏳ Mengumpulkan data... ({len(spreads)}/{LOOKBACK})")
                time.sleep(INTERVAL_SEC)
                continue

            z = zscore(np.array(spreads[-LOOKBACK:]))
            log(f"  📊 Z-score: {z:.4f} | Posisi: {position or 'NONE'}")

            # ── ENTRY LONG: beli BTC + ETH pakai USDT
            if position is None and z < -Z_ENTRY:
                qty_btc, qty_eth = calc_entry_qty(price_a, price_b)
                usdt_needed = (qty_btc * price_a + qty_eth * price_b) * 1.002
                usdt_avail  = get_balance("USDT")
                if qty_btc > 0 and qty_eth > 0 and usdt_avail >= usdt_needed:
                    log(f"  🟢 ENTRY LONG (z={z:.2f}) | buy {qty_btc} BTC + {qty_eth} ETH")
                    o1 = place_order(SYMBOL_A, "BUY", qty_btc)
                    o2 = place_order(SYMBOL_B, "BUY", qty_eth)
                    if o1 and o2:
                        position = "LONG_SPREAD"
                        trade_log.append({"time": datetime.now(), "action": "ENTER_LONG", "z": z})
                    else:
                        log("  ⚠️  Entry LONG gagal sebagian")
                else:
                    log(f"  ⏭️  LONG skip — USDT kurang (punya {usdt_avail:.2f}, butuh {usdt_needed:.2f})")

            # ── ENTRY SHORT: jual BTC + ETH, dapat USDT
            elif position is None and z > Z_ENTRY:
                qty_btc, qty_eth = calc_entry_qty(price_a, price_b)
                btc_avail = get_balance("BTC")
                eth_avail = get_balance("ETH")
                if btc_avail >= qty_btc and eth_avail >= qty_eth:
                    log(f"  🔴 ENTRY SHORT (z={z:.2f}) | sell {qty_btc} BTC + {qty_eth} ETH")
                    o1 = place_order(SYMBOL_A, "SELL", qty_btc)
                    o2 = place_order(SYMBOL_B, "SELL", qty_eth)
                    if o1 and o2:
                        position      = "SHORT_SPREAD"
                        short_qty_btc = qty_btc
                        short_qty_eth = qty_eth
                        trade_log.append({"time": datetime.now(), "action": "ENTER_SHORT", "z": z})
                    else:
                        log("  ⚠️  Entry SHORT gagal sebagian")
                else:
                    log("  ⏭️  SHORT skip — BTC/ETH kurang")

            # ── EXIT LONG: jual semua BTC+ETH yang ada (pakai balance aktual!)
            elif position == "LONG_SPREAD" and z > -Z_EXIT:
                log(f"  ⬛ EXIT LONG (z={z:.2f})")
                if sell_all_btc_eth():
                    position = None
                    trade_log.append({"time": datetime.now(), "action": "EXIT_LONG", "z": z})
                    print_balances()
                else:
                    log("  ⚠️  Exit LONG gagal, retry next tick...")

            # ── EXIT SHORT: beli kembali BTC+ETH
            elif position == "SHORT_SPREAD" and z < Z_EXIT:
                log(f"  ⬛ EXIT SHORT (z={z:.2f})")
                if buy_back_btc_eth(short_qty_btc, short_qty_eth):
                    position = None
                    trade_log.append({"time": datetime.now(), "action": "EXIT_SHORT", "z": z})
                    print_balances()
                else:
                    log("  ⚠️  Exit SHORT gagal, retry next tick...")

            # Trim history
            if len(hist_a) > LOOKBACK * 3:
                hist_a = hist_a[-LOOKBACK * 2:]
                hist_b = hist_b[-LOOKBACK * 2:]

        except BinanceAPIException as e:
            log(f"❌ Binance error: {e.message}")
        except KeyboardInterrupt:
            log("🛑 Bot dihentikan.")
            if trade_log:
                pd.DataFrame(trade_log).to_csv("trade_log.csv", index=False)
                log("📄 Trade log → trade_log.csv")
            break
        except Exception as e:
            log(f"❌ Error: {e}")

        time.sleep(INTERVAL_SEC)

if __name__ == "__main__":
    run_bot()
