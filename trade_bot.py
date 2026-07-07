#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADE ANALYZER BOT - Pandas Olmadan Çalışan Versiyon
=====================================================
Binance API'den veri çeker, teknik analiz yapar, SL/TP/Lot hesaplar.

Komutlar:
    /start - Botu başlat
    /help - Yardım menüsü
    /analyze <sembol> [zaman_dilimi] - Teknik analiz yap
    /price <sembol> - Anlık fiyat ve 24s verisi
    /alert <sembol> <hedef_fiyat> - Fiyat alarmı kur
    /alerts - Aktif alarmları listele
    /removealert <id> - Alarm sil
    /watchlist - İzleme listesi
    /addwatch <sembol> - İzleme listesine ekle
    /removewatch <sembol> - İzleme listesinden çıkar
    /settings - Ayarları göster
    /setbalance <miktar> - Bakiye ayarla
    /setrisk <yuzde> - Risk yüzdesi ayarla
    /setleverage <kaldıraç> - Kaldıraç ayarla

Deploy: Railway.app
"""

import logging
import requests
import json
import sqlite3
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# ==================== KONFIGURASYON ====================
TOKEN = os.environ.get("BOT_TOKEN", "8538187108:AAE9gPW0b9vL1RLlZQ9_SlwVTBw66mI5Epg")
OWNER_ID = int(os.environ.get("OWNER_ID", "7339222202"))
BINANCE_BASE_URL = "https://api.binance.com"
DB_PATH = "trade_bot.db"

DEFAULT_SETTINGS = {
    "balance": 1000.0,
    "risk_percent": 2.0,
    "leverage": 1.0,
    "default_interval": "1h"
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== VERİTABANI ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 1000,
            risk_percent REAL DEFAULT 2,
            leverage REAL DEFAULT 1,
            default_interval TEXT DEFAULT '1h',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            target_price REAL,
            condition TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            triggered INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS analysis_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            interval TEXT,
            signal TEXT,
            entry_price REAL,
            sl_price REAL,
            tp_price REAL,
            rr REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_user_settings(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance, risk_percent, leverage, default_interval FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"balance": row[0], "risk_percent": row[1], "leverage": row[2], "default_interval": row[3]}
    else:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        return DEFAULT_SETTINGS.copy()

def update_user_setting(user_id, key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE user_settings SET {key} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def add_alert(user_id, symbol, target_price, condition):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO alerts (user_id, symbol, target_price, condition) VALUES (?, ?, ?, ?)",
              (user_id, symbol.upper(), target_price, condition))
    conn.commit()
    alert_id = c.lastrowid
    conn.close()
    return alert_id

def get_user_alerts(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, symbol, target_price, condition, created_at FROM alerts WHERE user_id = ? AND triggered = 0", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def remove_alert(alert_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0

def mark_alert_triggered(alert_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()

def add_to_watchlist(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO watchlist (user_id, symbol) VALUES (?, ?)", (user_id, symbol.upper()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def remove_from_watchlist(user_id, symbol):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol.upper()))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0

def get_watchlist(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def save_analysis(user_id, symbol, interval, signal, entry, sl, tp, rr):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO analysis_history (user_id, symbol, interval, signal, entry_price, sl_price, tp_price, rr) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (user_id, symbol, interval, signal, entry, sl, tp, rr))
    conn.commit()
    conn.close()

# ==================== VERİ ÇEKME ====================
def fetch_klines(symbol, interval, limit=500):
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        # [open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
        candles = []
        for d in data:
            candles.append({
                "open_time": d[0],
                "open": float(d[1]),
                "high": float(d[2]),
                "low": float(d[3]),
                "close": float(d[4]),
                "volume": float(d[5]),
                "close_time": d[6],
                "quote_volume": float(d[7]),
                "trades": int(d[8])
            })
        return candles
    except Exception as e:
        logger.error(f"Kline hatası: {e}")
        return None

def fetch_ticker(symbol):
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    params = {"symbol": symbol.upper()}
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Ticker hatası: {e}")
        return None

# ==================== TEKNİK ANALİZ (Pure Python) ====================
def calculate_sma(prices, period):
    if len(prices) < period:
        return []
    sma = []
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1:i + 1]
        sma.append(sum(window) / period)
    return sma

def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for i in range(period, len(prices)):
        ema.append((prices[i] - ema[-1]) * multiplier + ema[-1])
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = [100 - (100 / (1 + avg_gain / avg_loss))] if avg_loss != 0 else [100]

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi.append(100)
        else:
            rsi.append(100 - (100 / (1 + avg_gain / avg_loss)))
    return rsi

def calculate_atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return []
    tr = []
    for i in range(1, len(highs)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i - 1])
        tr3 = abs(lows[i] - closes[i - 1])
        tr.append(max(tr1, tr2, tr3))

    atr = [sum(tr[:period]) / period]
    for i in range(period, len(tr)):
        atr.append((atr[-1] * (period - 1) + tr[i]) / period)
    return atr

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        return [], [], []
    upper, sma, lower = [], [], []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        sma.append(mean)
        upper.append(mean + 2 * std)
        lower.append(mean - 2 * std)
    return upper, sma, lower

def calculate_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    if not ema_fast or not ema_slow:
        return [], [], []
    offset = slow - fast
    macd = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_line = calculate_ema(macd, signal)
    hist = [macd[i + signal - 1] - signal_line[i] for i in range(len(signal_line))]
    return macd, signal_line, hist

def calculate_stochastic(highs, lows, closes, k_period=14, d_period=3):
    if len(closes) < k_period:
        return [], []
    k = []
    for i in range(k_period - 1, len(closes)):
        highest = max(highs[i - k_period + 1:i + 1])
        lowest = min(lows[i - k_period + 1:i + 1])
        if highest == lowest:
            k.append(50)
        else:
            k.append(100 * ((closes[i] - lowest) / (highest - lowest)))
    d = calculate_sma(k, d_period)
    return k, d

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return [], [], []
    tr = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    atr14 = [sum(tr[:period]) / period]
    plus_di14 = [sum(plus_dm[:period]) / period]
    minus_di14 = [sum(minus_dm[:period]) / period]

    for i in range(period, len(tr)):
        atr14.append((atr14[-1] * (period - 1) + tr[i]) / period)
        plus_di14.append((plus_di14[-1] * (period - 1) + plus_dm[i]) / period)
        minus_di14.append((minus_di14[-1] * (period - 1) + minus_dm[i]) / period)

    dx = []
    for i in range(len(atr14)):
        pdi = 100 * plus_di14[i] / atr14[i] if atr14[i] != 0 else 0
        mdi = 100 * minus_di14[i] / atr14[i] if atr14[i] != 0 else 0
        if pdi + mdi == 0:
            dx.append(0)
        else:
            dx.append(100 * abs(pdi - mdi) / (pdi + mdi))

    adx = [sum(dx[:period]) / period]
    for i in range(period, len(dx)):
        adx.append((adx[-1] * (period - 1) + dx[i]) / period)

    return adx, [100 * x / atr14[i] if atr14[i] != 0 else 0 for i, x in enumerate(plus_di14)], [100 * x / atr14[i] if atr14[i] != 0 else 0 for i, x in enumerate(minus_di14)]

def calculate_support_resistance(highs, lows, closes):
    pivot = (highs[-1] + lows[-1] + closes[-1]) / 3
    r1 = 2 * pivot - lows[-1]
    s1 = 2 * pivot - highs[-1]
    r2 = pivot + (highs[-1] - lows[-1])
    s2 = pivot - (highs[-1] - lows[-1])
    r3 = highs[-1] + 2 * (pivot - lows[-1])
    s3 = lows[-1] - 2 * (highs[-1] - pivot)
    return {"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2, "r3": r3, "s3": s3}

def calculate_fibonacci(high, low):
    diff = high - low
    return {
        '0.0%': high, '23.6%': high - 0.236 * diff, '38.2%': high - 0.382 * diff,
        '50.0%': high - 0.5 * diff, '61.8%': high - 0.618 * diff,
        '78.6%': high - 0.786 * diff, '100.0%': low
    }

def calculate_lot_size(balance, risk_percent, entry, stop_loss, leverage=1):
    risk_amount = balance * (risk_percent / 100)
    price_diff = abs(entry - stop_loss)
    if price_diff == 0:
        return 0
    return round(risk_amount / (price_diff * leverage), 6)

def calculate_rr(entry, sl, tp):
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk == 0:
        return 0
    return round(reward / risk, 2)

# ==================== ANALİZ MOTORU ====================
def perform_analysis(candles, settings):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi = calculate_rsi(closes)
    atr = calculate_atr(highs, lows, closes)
    bb_upper, bb_sma, bb_lower = calculate_bollinger(closes)
    macd, macd_signal, macd_hist = calculate_macd(closes)
    stoch_k, stoch_d = calculate_stochastic(highs, lows, closes)
    adx, plus_di, minus_di = calculate_adx(highs, lows, closes)
    sr = calculate_support_resistance(highs, lows, closes)
    fib = calculate_fibonacci(max(highs), min(lows))
    sma_20 = calculate_sma(closes, 20)
    sma_50 = calculate_sma(closes, 50)
    sma_200 = calculate_sma(closes, 200)

    last_close = closes[-1]
    last_rsi = rsi[-1] if rsi else 50
    last_atr = atr[-1] if atr else 0
    last_macd = macd[-1] if macd else 0
    last_macd_signal = macd_signal[-1] if macd_signal else 0
    last_macd_hist = macd_hist[-1] if macd_hist else 0
    last_bb_upper = bb_upper[-1] if bb_upper else last_close
    last_bb_lower = bb_lower[-1] if bb_lower else last_close
    last_stoch_k = stoch_k[-1] if stoch_k else 50
    last_stoch_d = stoch_d[-1] if stoch_d else 50
    last_adx = adx[-1] if adx else 0
    last_plus_di = plus_di[-1] if plus_di else 0
    last_minus_di = minus_di[-1] if minus_di else 0

    last_sma_20 = sma_20[-1] if sma_20 else last_close
    last_sma_50 = sma_50[-1] if sma_50 else last_close
    last_sma_200 = sma_200[-1] if sma_200 else last_close

    trend_dir = "YUKARI" if last_close > last_sma_20 > last_sma_50 else                 "AŞAĞI" if last_close < last_sma_20 < last_sma_50 else "YAN"

    volatility = (last_atr / last_close) * 100 if last_close != 0 else 0

    # Sinyaller
    signals = []
    score = 0

    if last_rsi < 30: signals.append(("RSI", "AŞIRI SATIŞ → ALIŞ", 2)); score += 2
    elif last_rsi > 70: signals.append(("RSI", "AŞIRI ALIŞ → SATIŞ", -2)); score -= 2
    elif last_rsi < 40: signals.append(("RSI", "DÜŞÜK → ALIŞ", 1)); score += 1
    elif last_rsi > 60: signals.append(("RSI", "YÜKSEK → SATIŞ", -1)); score -= 1
    else: signals.append(("RSI", "NÖTR", 0))

    prev_macd_hist = macd_hist[-2] if len(macd_hist) > 1 else 0
    if last_macd > last_macd_signal and last_macd_hist > prev_macd_hist:
        signals.append(("MACD", "YUKARI DÖNÜŞ → ALIŞ", 2)); score += 2
    elif last_macd < last_macd_signal and last_macd_hist < prev_macd_hist:
        signals.append(("MACD", "AŞAĞI DÖNÜŞ → SATIŞ", -2)); score -= 2
    elif last_macd > last_macd_signal:
        signals.append(("MACD", "POZİTİF → ALIŞ", 1)); score += 1
    else:
        signals.append(("MACD", "NEGATİF → SATIŞ", -1)); score -= 1

    if last_close < last_bb_lower:
        signals.append(("Bollinger", "ALT BANT KIRILIMI → ALIŞ", 2)); score += 2
    elif last_close > last_bb_upper:
        signals.append(("Bollinger", "ÜST BANT KIRILIMI → SATIŞ", -2)); score -= 2
    elif last_close > bb_sma[-1] if bb_sma else last_close:
        signals.append(("Bollinger", "BANT ÜSTÜ → ALIŞ", 1)); score += 1
    else:
        signals.append(("Bollinger", "BANT ALTINDA → SATIŞ", -1)); score -= 1

    if trend_dir == "YUKARI": signals.append(("Trend", "YUKARI TREND", 2)); score += 2
    elif trend_dir == "AŞAĞI": signals.append(("Trend", "AŞAĞI TREND", -2)); score -= 2
    else: signals.append(("Trend", "YAN BANT", 0))

    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
    last_vol = volumes[-1]
    if last_vol > avg_vol * 1.5: signals.append(("Hacim", "YÜKSEK HACİM → ONAY", 1)); score += 1
    else: signals.append(("Hacim", "NORMAL HACİM", 0))

    if last_stoch_k < 20 and last_stoch_d < 20:
        signals.append(("Stochastic", "AŞIRI SATIŞ → ALIŞ", 2)); score += 2
    elif last_stoch_k > 80 and last_stoch_d > 80:
        signals.append(("Stochastic", "AŞIRI ALIŞ → SATIŞ", -2)); score -= 2
    elif last_stoch_k > last_stoch_d:
        signals.append(("Stochastic", "YUKARI KESİŞİM → ALIŞ", 1)); score += 1
    else:
        signals.append(("Stochastic", "AŞAĞI KESİŞİM → SATIŞ", -1)); score -= 1

    if last_adx > 25 and last_plus_di > last_minus_di:
        signals.append(("ADX", "GÜÇLÜ YUKARI TREND", 2)); score += 2
    elif last_adx > 25 and last_plus_di < last_minus_di:
        signals.append(("ADX", "GÜÇLÜ AŞAĞI TREND", -2)); score -= 2
    elif last_adx > 20: signals.append(("ADX", "TREND GELİŞİYOR", 0))
    else: signals.append(("ADX", "YAN BANT", 0))

    if score >= 6: overall = "GÜÇLÜ ALIŞ"
    elif score >= 3: overall = "ALIŞ"
    elif score <= -6: overall = "GÜÇLÜ SATIŞ"
    elif score <= -3: overall = "SATIŞ"
    else: overall = "NÖTR / BEKLE"

    is_buy = "ALIŞ" in overall
    is_sell = "SATIŞ" in overall

    entry = last_close
    balance = settings["balance"]
    risk_p = settings["risk_percent"]
    leverage = settings["leverage"]

    if is_buy:
        sl_atr = entry - last_atr * 1.5
        tp_atr = entry + last_atr * 3.0
        sl_sr = sr["s1"] if sr["s1"] < entry else entry - last_atr * 2
        tp_sr = sr["r1"] if sr["r1"] > entry else entry + last_atr * 2
    else:
        sl_atr = entry + last_atr * 1.5
        tp_atr = entry - last_atr * 3.0
        sl_sr = sr["r1"] if sr["r1"] > entry else entry + last_atr * 2
        tp_sr = sr["s1"] if sr["s1"] < entry else entry - last_atr * 2

    rr_atr = calculate_rr(entry, sl_atr, tp_atr)
    rr_sr = calculate_rr(entry, sl_sr, tp_sr)
    lot_atr = calculate_lot_size(balance, risk_p, entry, sl_atr, leverage)
    lot_sr = calculate_lot_size(balance, risk_p, entry, sl_sr, leverage)

    pos_val_atr = lot_atr * entry * leverage
    margin_atr = pos_val_atr / leverage

    warnings = []
    if last_rsi < 20 or last_rsi > 80: warnings.append("⚠️ RSI aşırı bölgede")
    if last_adx < 20: warnings.append("⚠️ ADX düşük - trend güçsüz")
    if volatility > 5: warnings.append("⚠️ Yüksek volatilite")
    if last_vol < avg_vol * 0.5: warnings.append("⚠️ Düşük hacim")
    if leverage > 10: warnings.append(f"⚠️ Yüksek kaldıraç ({leverage}x)")
    if rr_atr < 1.5: warnings.append("⚠️ Düşük R/R oranı")

    return {
        "last_close": last_close, "rsi": last_rsi, "atr": last_atr,
        "macd": last_macd, "macd_signal": last_macd_signal, "macd_hist": last_macd_hist,
        "sma_20": last_sma_20, "sma_50": last_sma_50, "sma_200": last_sma_200,
        "stoch_k": last_stoch_k, "stoch_d": last_stoch_d,
        "adx": last_adx, "plus_di": last_plus_di, "minus_di": last_minus_di,
        "bb_upper": last_bb_upper, "bb_lower": last_bb_lower,
        "trend_dir": trend_dir, "volatility": volatility,
        "sr": sr, "fib": fib, "signals": signals, "score": score,
        "overall": overall, "is_buy": is_buy, "is_sell": is_sell,
        "entry": entry, "sl_atr": sl_atr, "tp_atr": tp_atr, "rr_atr": rr_atr, "lot_atr": lot_atr,
        "sl_sr": sl_sr, "tp_sr": tp_sr, "rr_sr": rr_sr, "lot_sr": lot_sr,
        "warnings": warnings, "balance": balance, "risk_p": risk_p, "leverage": leverage,
        "pos_val_atr": pos_val_atr, "margin_atr": margin_atr
    }

def format_analysis_report(symbol, interval, result, ticker_data=None):
    emoji_signal = "🟢" if result["is_buy"] else "🔴" if result["is_sell"] else "⚪"

    report = f"""
{emoji_signal} <b>ANALİZ RAPORU</b> {emoji_signal}

📊 <b>{symbol.upper()}</b> | {interval}
💰 Son Fiyat: <code>${result['last_close']:,.2f}</code>
📈 RSI(14): <code>{result['rsi']:.2f}</code>
📉 ATR(14): <code>${result['atr']:,.2f}</code>
📊 Volatilite: <code>{result['volatility']:.2f}%</code>
📈 Trend: <code>{result['trend_dir']}</code>

<b>🔔 GENEL SİNYAL: {result['overall']}</b>
Sinyal Skoru: {result['score']:+d}

<b>📈 GÖSTERGELER</b>
MACD: {result['macd']:.2f}
MACD Sinyal: {result['macd_signal']:.2f}
SMA 20: ${result['sma_20']:,.2f}
SMA 50: ${result['sma_50']:,.2f}
SMA 200: ${result['sma_200']:,.2f}
Stoch K: {result['stoch_k']:.2f}
Stoch D: {result['stoch_d']:.2f}
ADX: {result['adx']:.2f}
+DI: {result['plus_di']:.2f} | -DI: {result['minus_di']:.2f}
BB Üst: ${result['bb_upper']:,.2f}
BB Alt: ${result['bb_lower']:,.2f}

<b>🎯 SEVİYELER</b>
R3: ${result['sr']['r3']:,.2f}
R2: ${result['sr']['r2']:,.2f}
R1: ${result['sr']['r1']:,.2f}
Pivot: ${result['sr']['pivot']:,.2f}
S1: ${result['sr']['s1']:,.2f}
S2: ${result['sr']['s2']:,.2f}
S3: ${result['sr']['s3']:,.2f}

<b>💡 POZİSYON (ATR Bazlı)</b>
Giriş: <code>${result['entry']:,.2f}</code>
SL: <code>${result['sl_atr']:,.2f}</code> ({abs((result['sl_atr']-result['entry'])/result['entry']*100):.2f}%)
TP: <code>${result['tp_atr']:,.2f}</code> ({abs((result['tp_atr']-result['entry'])/result['entry']*100):.2f}%)
R/R: <code>1:{result['rr_atr']}</code>
Lot: <code>{result['lot_atr']}</code>
Poz. Değer: <code>${result['pos_val_atr']:,.2f}</code>
Marjin: <code>${result['margin_atr']:,.2f}</code>

<b>💡 POZİSYON (Destek/Direnç)</b>
SL: <code>${result['sl_sr']:,.2f}</code>
TP: <code>${result['tp_sr']:,.2f}</code>
R/R: <code>1:{result['rr_sr']}</code>
Lot: <code>{result['lot_sr']}</code>

<b>⚙️ AYARLAR</b>
Bakiye: ${result['balance']:,.2f}
Risk: %{result['risk_p']}
Kaldıraç: {result['leverage']}x
"""

    if ticker_data:
        report += f"""
<b>📊 24S İSTATİSTİKLER</b>
Değişim: %{float(ticker_data.get('priceChangePercent', 0)):+.2f}
Yüksek: ${float(ticker_data.get('highPrice', 0)):,.2f}
Düşük: ${float(ticker_data.get('lowPrice', 0)):,.2f}
Hacim: {float(ticker_data.get('volume', 0)):,.4f}
"""

    if result["warnings"]:
        report += "
<b>⚠️ UYARILAR</b>
" + "
".join(result["warnings"])

    return report

# ==================== KOMUT İŞLEYİCİLER ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user_settings(user_id)

    welcome_text = """
🤖 <b>TRADE ANALYZER BOT</b>

Merhaba! Ben senin teknik analiz asistanınım.

<b>Ne yapabilirim?</b>
• 📊 Teknik analiz (RSI, MACD, Bollinger, ADX, Stochastic...)
• 🎯 Stop Loss / Take Profit / Lot hesaplama
• 🔔 Fiyat alarmları kurma
• 📈 İzleme listesi takibi

<b>Başlangıç komutları:</b>
/analyze BTCUSDT - Analiz yap
/price BTCUSDT - Anlık fiyat
/settings - Ayarlarını gör
/help - Tüm komutlar

<b>⚠️ Uyarı:</b> Bu bot eğitim amaçlıdır. Yatırım tavsiyesi değildir.
"""
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
<b>📚 KOMUT LİSTESİ</b>

<b>🔍 Analiz</b>
/analyze &lt;sembol&gt; [zaman_dilimi]
  Örnek: /analyze BTCUSDT
  Örnek: /analyze ETHUSDT 4h

<b>💰 Fiyat</b>
/price &lt;sembol&gt;
  Örnek: /price BTCUSDT

<b>🔔 Alarmlar</b>
/alert &lt;sembol&gt; &lt;hedef_fiyat&gt;
  Örnek: /alert BTCUSDT 70000
/alerts - Aktif alarmlar
/removealert &lt;id&gt; - Alarm sil

<b>📈 İzleme Listesi</b>
/watchlist - Listeyi gör
/addwatch &lt;sembol&gt; - Ekle
/removewatch &lt;sembol&gt; - Çıkar

<b>⚙️ Ayarlar</b>
/settings - Mevcut ayarlar
/setbalance &lt;miktar&gt; - Bakiye
/setrisk &lt;yuzde&gt; - Risk %
/setleverage &lt;kaldıraç&gt; - Kaldıraç

<b>Zaman Dilimleri:</b> 1m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w
"""
    await update.message.reply_text(help_text, parse_mode="HTML")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Kullanım: /analyze &lt;sembol&gt; [zaman_dilimi]
"
            "Örnek: /analyze BTCUSDT
"
            "Örnek: /analyze ETHUSDT 4h",
            parse_mode="HTML"
        )
        return

    symbol = args[0].upper()
    interval = args[1] if len(args) > 1 else settings["default_interval"]

    loading_msg = await update.message.reply_text(f"🔄 {symbol} analiz ediliyor...")

    candles = fetch_klines(symbol, interval)
    if not candles:
        await loading_msg.edit_text("❌ Veri çekilemedi. Sembolü kontrol edin.")
        return

    ticker = fetch_ticker(symbol)
    result = perform_analysis(candles, settings)
    report = format_analysis_report(symbol, interval, result, ticker)

    save_analysis(user_id, symbol, interval, result["overall"], 
                  result["entry"], result["sl_atr"], result["tp_atr"], result["rr_atr"])

    keyboard = [
        [InlineKeyboardButton("🔄 Yenile", callback_data=f"refresh:{symbol}:{interval}")],
        [InlineKeyboardButton("➕ İzleme Listesine Ekle", callback_data=f"addwatch:{symbol}")],
        [InlineKeyboardButton("🔔 Alarm Kur", callback_data=f"alert:{symbol}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await loading_msg.edit_text(report, parse_mode="HTML", reply_markup=reply_markup)

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /price &lt;sembol&gt;
Örnek: /price BTCUSDT", parse_mode="HTML")
        return

    symbol = args[0].upper()
    ticker = fetch_ticker(symbol)

    if not ticker:
        await update.message.reply_text("❌ Veri çekilemedi.")
        return

    price = float(ticker.get("lastPrice", 0))
    change = float(ticker.get("priceChangePercent", 0))
    high = float(ticker.get("highPrice", 0))
    low = float(ticker.get("lowPrice", 0))
    vol = float(ticker.get("volume", 0))
    quote_vol = float(ticker.get("quoteVolume", 0))

    emoji = "🟢" if change >= 0 else "🔴"

    text = f"""
{emoji} <b>{symbol}</b>

💰 Fiyat: <code>${price:,.2f}</code>
📊 24s Değişim: <code>{change:+.2f}%</code>
📈 24s Yüksek: <code>${high:,.2f}</code>
📉 24s Düşük: <code>${low:,.2f}</code>
📦 Hacim: <code>{vol:,.4f}</code>
💵 İşlem Hacmi: <code>${quote_vol:,.2f}</code>

<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>
"""

    keyboard = [[InlineKeyboardButton("📊 Analiz Et", callback_data=f"analyze:{symbol}")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Kullanım: /alert &lt;sembol&gt; &lt;hedef_fiyat&gt;
"
            "Örnek: /alert BTCUSDT 70000",
            parse_mode="HTML"
        )
        return

    user_id = update.effective_user.id
    symbol = args[0].upper()

    try:
        target_price = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz fiyat.")
        return

    ticker = fetch_ticker(symbol)
    if not ticker:
        await update.message.reply_text("❌ Sembol bulunamadı.")
        return

    current_price = float(ticker.get("lastPrice", 0))
    condition = "above" if target_price > current_price else "below"

    alert_id = add_alert(user_id, symbol, target_price, condition)

    direction = "üzerine çıkarsa" if condition == "above" else "altına düşerse"
    await update.message.reply_text(
        f"✅ Alarm #{alert_id} kuruldu!

"
        f"📊 {symbol}
"
        f"💰 Hedef: ${target_price:,.2f}
"
        f"📈 Mevcut: ${current_price:,.2f}
"
        f"🎯 Koşul: Fiyat {direction} alarm verecek.",
        parse_mode="HTML"
    )

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    alerts = get_user_alerts(user_id)

    if not alerts:
        await update.message.reply_text("📭 Aktif alarmınız yok.
/alarm kurmak için: /alert BTCUSDT 70000")
        return

    text = "🔔 <b>AKTİF ALARMLARINIZ</b>

"
    for alert in alerts:
        alert_id, uid, sym, target, condition, created = alert
        direction = "📈 Yukarı" if condition == "above" else "📉 Aşağı"
        text += f"#{alert_id} | {sym} | ${target:,.2f} | {direction}
"

    text += "
Silme: /removealert &lt;id&gt;"
    await update.message.reply_text(text, parse_mode="HTML")

async def removealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /removealert &lt;id&gt;
Örnek: /removealert 1")
        return

    user_id = update.effective_user.id
    try:
        alert_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Geçersiz ID.")
        return

    if remove_alert(alert_id, user_id):
        await update.message.reply_text(f"✅ Alarm #{alert_id} silindi.")
    else:
        await update.message.reply_text("❌ Alarm bulunamadı.")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    symbols = get_watchlist(user_id)

    if not symbols:
        await update.message.reply_text("📭 İzleme listeniz boş.
Ekle: /addwatch BTCUSDT")
        return

    text = "📈 <b>İZLEME LİSTENİZ</b>

"
    for sym in symbols:
        ticker = fetch_ticker(sym)
        if ticker:
            price = float(ticker.get("lastPrice", 0))
            change = float(ticker.get("priceChangePercent", 0))
            emoji = "🟢" if change >= 0 else "🔴"
            text += f"{emoji} {sym}: ${price:,.2f} ({change:+.2f}%)
"
        else:
            text += f"⚪ {sym}: Veri yok
"

    keyboard = [[InlineKeyboardButton("🔄 Yenile", callback_data="refresh_watchlist")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def addwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /addwatch &lt;sembol&gt;
Örnek: /addwatch BTCUSDT")
        return

    user_id = update.effective_user.id
    symbol = args[0].upper()

    ticker = fetch_ticker(symbol)
    if not ticker:
        await update.message.reply_text(f"❌ {symbol} bulunamadı.")
        return

    if add_to_watchlist(user_id, symbol):
        await update.message.reply_text(f"✅ {symbol} izleme listesine eklendi.")
    else:
        await update.message.reply_text(f"⚠️ {symbol} zaten listede.")

async def removewatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /removewatch &lt;sembol&gt;")
        return

    user_id = update.effective_user.id
    symbol = args[0].upper()

    if remove_from_watchlist(user_id, symbol):
        await update.message.reply_text(f"✅ {symbol} listeden çıkarıldı.")
    else:
        await update.message.reply_text(f"❌ {symbol} listede bulunamadı.")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    text = f"""
⚙️ <b>AYARLARINIZ</b>

💰 Bakiye: ${settings['balance']:,.2f}
📊 Risk: %{settings['risk_percent']}
🔧 Kaldıraç: {settings['leverage']}x
⏱️ Varsayılan Zaman: {settings['default_interval']}

<b>Değiştir:</b>
/setbalance 5000
/setrisk 3
/setleverage 5
"""
    await update.message.reply_text(text, parse_mode="HTML")

async def setbalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /setbalance &lt;miktar&gt;
Örnek: /setbalance 5000")
        return

    try:
        balance = float(args[0])
        if balance <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Geçersiz miktar.")
        return

    user_id = update.effective_user.id
    update_user_setting(user_id, "balance", balance)
    await update.message.reply_text(f"✅ Bakiye ${balance:,.2f} olarak ayarlandı.")

async def setrisk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /setrisk &lt;yuzde&gt;
Örnek: /setrisk 3")
        return

    try:
        risk = float(args[0])
        if risk <= 0 or risk > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Geçersiz risk yüzdesi (0.1-100).")
        return

    user_id = update.effective_user.id
    update_user_setting(user_id, "risk_percent", risk)
    await update.message.reply_text(f"✅ Risk %{risk} olarak ayarlandı.")

async def setleverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /setleverage &lt;kaldıraç&gt;
Örnek: /setleverage 5")
        return

    try:
        leverage = float(args[0])
        if leverage < 1 or leverage > 125:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Geçersiz kaldıraç (1-125).")
        return

    user_id = update.effective_user.id
    update_user_setting(user_id, "leverage", leverage)
    await update.message.reply_text(f"✅ Kaldıraç {leverage}x olarak ayarlandı.")

# ==================== CALLBACK HANDLERLAR ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    if data.startswith("refresh:"):
        _, symbol, interval = data.split(":")
        candles = fetch_klines(symbol, interval)
        if not candles:
            await query.edit_message_text("❌ Veri çekilemedi.")
            return

        ticker = fetch_ticker(symbol)
        result = perform_analysis(candles, settings)
        report = format_analysis_report(symbol, interval, result, ticker)

        keyboard = [
            [InlineKeyboardButton("🔄 Yenile", callback_data=f"refresh:{symbol}:{interval}")],
            [InlineKeyboardButton("➕ İzleme Listesine Ekle", callback_data=f"addwatch:{symbol}")],
            [InlineKeyboardButton("🔔 Alarm Kur", callback_data=f"alert:{symbol}")]
        ]
        await query.edit_message_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("analyze:"):
        symbol = data.split(":")[1]
        interval = settings["default_interval"]
        candles = fetch_klines(symbol, interval)
        if not candles:
            await query.edit_message_text("❌ Veri çekilemedi.")
            return

        ticker = fetch_ticker(symbol)
        result = perform_analysis(candles, settings)
        report = format_analysis_report(symbol, interval, result, ticker)

        keyboard = [
            [InlineKeyboardButton("🔄 Yenile", callback_data=f"refresh:{symbol}:{interval}")],
            [InlineKeyboardButton("➕ İzleme Listesine Ekle", callback_data=f"addwatch:{symbol}")]
        ]
        await query.edit_message_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("addwatch:"):
        symbol = data.split(":")[1]
        if add_to_watchlist(user_id, symbol):
            await query.edit_message_text(f"✅ {symbol} izleme listesine eklendi.")
        else:
            await query.edit_message_text(f"⚠️ {symbol} zaten listede.")

    elif data.startswith("alert:"):
        symbol = data.split(":")[1]
        await query.edit_message_text(
            f"🔔 Alarm kurmak için:
/alert {symbol} &lt;hedef_fiyat&gt;

"
            f"Örnek: /alert {symbol} 70000",
            parse_mode="HTML"
        )

    elif data == "refresh_watchlist":
        symbols = get_watchlist(user_id)
        if not symbols:
            await query.edit_message_text("📭 İzleme listeniz boş.")
            return

        text = "📈 <b>İZLEME LİSTENİZ</b>

"
        for sym in symbols:
            ticker = fetch_ticker(sym)
            if ticker:
                price = float(ticker.get("lastPrice", 0))
                change = float(ticker.get("priceChangePercent", 0))
                emoji = "🟢" if change >= 0 else "🔴"
                text += f"{emoji} {sym}: ${price:,.2f} ({change:+.2f}%)
"
            else:
                text += f"⚪ {sym}: Veri yok
"

        keyboard = [[InlineKeyboardButton("🔄 Yenile", callback_data="refresh_watchlist")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==================== ALARM KONTROLÜ ====================
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, symbol, target_price, condition FROM alerts WHERE triggered = 0")
    alerts = c.fetchall()
    conn.close()

    for alert in alerts:
        alert_id, user_id, symbol, target_price, condition = alert

        ticker = fetch_ticker(symbol)
        if not ticker:
            continue

        current_price = float(ticker.get("lastPrice", 0))

        triggered = False
        if condition == "above" and current_price >= target_price:
            triggered = True
            direction = "📈 yukarı çıktı"
        elif condition == "below" and current_price <= target_price:
            triggered = True
            direction = "📉 aşağı düştü"

        if triggered:
            mark_alert_triggered(alert_id)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🚨 <b>ALARM!</b>

"
                         f"📊 {symbol}
"
                         f"🎯 Hedef: ${target_price:,.2f}
"
                         f"💰 Mevcut: ${current_price:,.2f}
"
                         f"📈 Fiyat {direction}!

"
                         f"/analyze {symbol} ile analiz yapabilirsin.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Alarm mesajı gönderilemedi: {e}")

# ==================== ANA PROGRAM ====================
def main():
    init_db()

    if TOKEN == "YOUR_BOT_TOKEN_HERE" or not TOKEN:
        print("❌ BOT_TOKEN çevre değişkeni ayarlanmamış!")
        print("export BOT_TOKEN=senin_bot_tokenin")
        print("export OWNER_ID=senin_telegram_id_n")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("removealert", removealert_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("addwatch", addwatch_command))
    application.add_handler(CommandHandler("removewatch", removewatch_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("setbalance", setbalance_command))
    application.add_handler(CommandHandler("setrisk", setrisk_command))
    application.add_handler(CommandHandler("setleverage", setleverage_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    application.job_queue.run_repeating(check_alerts, interval=120, first=10)

    print("🤖 Trade Analyzer Bot başlatılıyor...")
    print("💡 Komutlar: /start, /help, /analyze, /price, /alert")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
