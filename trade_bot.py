#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TRADE ANALYZER BOT - Telegram Teknik Analiz Botu
=================================================
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

Örnek:
    /analyze BTCUSDT
    /analyze ETHUSDT 4h
    /alert BTCUSDT 70000
    /setbalance 5000
    /setrisk 3

Deploy: Railway.app (Procfile: worker: python trade_bot.py)
"""

import logging
import requests
import pandas as pd
import numpy as np
import json
import sqlite3
import os
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# ==================== KONFIGURASYON ====================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
BINANCE_BASE_URL = "https://api.binance.com"
DB_PATH = "trade_bot.db"

# Varsayılan ayarlar
DEFAULT_SETTINGS = {
    "balance": 1000.0,
    "risk_percent": 2.0,
    "leverage": 1.0,
    "default_interval": "1h"
}

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== VERİTABANI ====================
def init_db():
    """Veritabanını başlat"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Kullanıcı ayarları
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

    # Alarmlar
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            target_price REAL,
            condition TEXT,  -- 'above' veya 'below'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            triggered INTEGER DEFAULT 0
        )
    """)

    # İzleme listesi
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Analiz geçmişi
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
    """Kullanıcı ayarlarını getir"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance, risk_percent, leverage, default_interval FROM user_settings WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row:
        return {
            "balance": row[0],
            "risk_percent": row[1],
            "leverage": row[2],
            "default_interval": row[3]
        }
    else:
        # Varsayılan ayarları kaydet
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        return DEFAULT_SETTINGS.copy()

def update_user_setting(user_id, key, value):
    """Kullanıcı ayarını güncelle"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE user_settings SET {key} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def add_alert(user_id, symbol, target_price, condition):
    """Alarm ekle"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO alerts (user_id, symbol, target_price, condition) VALUES (?, ?, ?, ?)",
              (user_id, symbol.upper(), target_price, condition))
    conn.commit()
    alert_id = c.lastrowid
    conn.close()
    return alert_id

def get_user_alerts(user_id):
    """Kullanıcı alarmlarını getir"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, symbol, target_price, condition, created_at FROM alerts WHERE user_id = ? AND triggered = 0", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def remove_alert(alert_id, user_id):
    """Alarm sil"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0

def mark_alert_triggered(alert_id):
    """Alarmı tetiklendi olarak işaretle"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()

def add_to_watchlist(user_id, symbol):
    """İzleme listesine ekle"""
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
    """İzleme listesinden çıkar"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol.upper()))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted > 0

def get_watchlist(user_id):
    """İzleme listesini getir"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def save_analysis(user_id, symbol, interval, signal, entry, sl, tp, rr):
    """Analizi kaydet"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO analysis_history (user_id, symbol, interval, signal, entry_price, sl_price, tp_price, rr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, symbol, interval, signal, entry, sl, tp, rr))
    conn.commit()
    conn.close()

# ==================== TEKNİK ANALİZ ====================
def fetch_klines(symbol, interval, limit=500):
    """Binance'ten kline verisi çek"""
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume', 'trades']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        return df
    except Exception as e:
        logger.error(f"Kline hatası: {e}")
        return None

def fetch_ticker(symbol):
    """24s ticker verisi"""
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    params = {"symbol": symbol.upper()}
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Ticker hatası: {e}")
        return None

def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_bollinger(close, period=20, std_dev=2):
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return sma + std * std_dev, sma, sma - std * std_dev

def calculate_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    return macd, macd_signal, macd - macd_signal

def calculate_stochastic(high, low, close, k_period=14, d_period=3):
    lowest = low.rolling(window=k_period).min()
    highest = high.rolling(window=k_period).max()
    k = 100 * ((close - lowest) / (highest - lowest))
    return k, k.rolling(window=d_period).mean()

def calculate_adx(high, low, close, period=14):
    plus_dm = high.diff()
    minus_dm = low.diff().abs()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    atr = calculate_atr(high, low, close, period)
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    return dx.rolling(period).mean(), plus_di, minus_di

def calculate_support_resistance(high, low, close):
    pivot = (high.iloc[-1] + low.iloc[-1] + close.iloc[-1]) / 3
    r1 = 2 * pivot - low.iloc[-1]
    s1 = 2 * pivot - high.iloc[-1]
    r2 = pivot + (high.iloc[-1] - low.iloc[-1])
    s2 = pivot - (high.iloc[-1] - low.iloc[-1])
    r3 = high.iloc[-1] + 2 * (pivot - low.iloc[-1])
    s3 = low.iloc[-1] - 2 * (high.iloc[-1] - pivot)
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

def perform_analysis(df, close, high, low, volume, settings):
    """Tam teknik analiz"""
    rsi = calculate_rsi(close)
    atr = calculate_atr(high, low, close)
    bb_upper, bb_sma, bb_lower = calculate_bollinger(close)
    macd, macd_signal, macd_hist = calculate_macd(close)
    stoch_k, stoch_d = calculate_stochastic(high, low, close)
    adx, plus_di, minus_di = calculate_adx(high, low, close)
    sr = calculate_support_resistance(high, low, close)
    fib = calculate_fibonacci(high.max(), low.min())

    last_close = close.iloc[-1]
    last_rsi = rsi.iloc[-1]
    last_atr = atr.iloc[-1]
    last_macd = macd.iloc[-1]
    last_macd_signal = macd_signal.iloc[-1]
    last_macd_hist = macd_hist.iloc[-1]
    last_bb_upper = bb_upper.iloc[-1]
    last_bb_lower = bb_lower.iloc[-1]
    last_stoch_k = stoch_k.iloc[-1]
    last_stoch_d = stoch_d.iloc[-1]
    last_adx = adx.iloc[-1]
    last_plus_di = plus_di.iloc[-1]
    last_minus_di = minus_di.iloc[-1]

    sma_20 = close.rolling(20).mean().iloc[-1]
    sma_50 = close.rolling(50).mean().iloc[-1]
    sma_200 = close.rolling(200).mean().iloc[-1]

    trend_dir = "YUKARI" if last_close > sma_20 > sma_50 else                 "AŞAĞI" if last_close < sma_20 < sma_50 else "YAN"

    volatility = (last_atr / last_close) * 100

    # Sinyaller
    signals = []
    score = 0

    if last_rsi < 30: signals.append(("RSI", "AŞIRI SATIŞ → ALIŞ", 2)); score += 2
    elif last_rsi > 70: signals.append(("RSI", "AŞIRI ALIŞ → SATIŞ", -2)); score -= 2
    elif last_rsi < 40: signals.append(("RSI", "DÜŞÜK → ALIŞ", 1)); score += 1
    elif last_rsi > 60: signals.append(("RSI", "YÜKSEK → SATIŞ", -1)); score -= 1
    else: signals.append(("RSI", "NÖTR", 0))

    if last_macd > last_macd_signal and macd_hist.iloc[-1] > macd_hist.iloc[-2]:
        signals.append(("MACD", "YUKARI DÖNÜŞ → ALIŞ", 2)); score += 2
    elif last_macd < last_macd_signal and macd_hist.iloc[-1] < macd_hist.iloc[-2]:
        signals.append(("MACD", "AŞAĞI DÖNÜŞ → SATIŞ", -2)); score -= 2
    elif last_macd > last_macd_signal:
        signals.append(("MACD", "POZİTİF → ALIŞ", 1)); score += 1
    else:
        signals.append(("MACD", "NEGATİF → SATIŞ", -1)); score -= 1

    if last_close < last_bb_lower:
        signals.append(("Bollinger", "ALT BANT KIRILIMI → ALIŞ", 2)); score += 2
    elif last_close > last_bb_upper:
        signals.append(("Bollinger", "ÜST BANT KIRILIMI → SATIŞ", -2)); score -= 2
    elif last_close > bb_sma.iloc[-1]:
        signals.append(("Bollinger", "BANT ÜSTÜ → ALIŞ", 1)); score += 1
    else:
        signals.append(("Bollinger", "BANT ALTINDA → SATIŞ", -1)); score -= 1

    if trend_dir == "YUKARI": signals.append(("Trend", "YUKARI TREND", 2)); score += 2
    elif trend_dir == "AŞAĞI": signals.append(("Trend", "AŞAĞI TREND", -2)); score -= 2
    else: signals.append(("Trend", "YAN BANT", 0))

    avg_vol = volume.rolling(20).mean().iloc[-1]
    last_vol = volume.iloc[-1]
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

    # Genel sinyal
    if score >= 6: overall = "GÜÇLÜ ALIŞ"
    elif score >= 3: overall = "ALIŞ"
    elif score <= -6: overall = "GÜÇLÜ SATIŞ"
    elif score <= -3: overall = "SATIŞ"
    else: overall = "NÖTR / BEKLE"

    is_buy = "ALIŞ" in overall
    is_sell = "SATIŞ" in overall

    # Pozisyon hesaplama
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

    # Uyarılar
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
        "sma_20": sma_20, "sma_50": sma_50, "sma_200": sma_200,
        "stoch_k": last_stoch_k, "stoch_d": last_stoch_d,
        "adx": last_adx, "plus_di": last_plus_di, "minus_di": last_minus_di,
        "bb_upper": last_bb_upper, "bb_lower": last_bb_lower,
        "trend_dir": trend_dir, "volatility": volatility,
        "sr": sr, "fib": fib, "signals": signals, "score": score,
        "overall": overall, "is_buy": is_buy, "is_sell": is_sell,
        "entry": entry, "sl_atr": sl_atr, "tp_atr": tp_atr, "rr_atr": rr_atr, "lot_atr": lot_atr,
        "sl_sr": sl_sr, "tp_sr": tp_sr, "rr_sr": rr_sr, "lot_sr": lot_sr,
        "warnings": warnings, "balance": balance, "risk_p": risk_p, "leverage": leverage
    }

def format_analysis_report(symbol, interval, result, ticker_data=None):
    """Analiz raporunu formatla"""
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
Poz. Değer: <code>${result['lot_atr'] * result['entry'] * result['leverage']:,.2f}</code>
Marjin: <code>${result['lot_atr'] * result['entry']:,.2f}</code>

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
    """Bot başlatma"""
    user_id = update.effective_user.id
    get_user_settings(user_id)  # Varsayılan ayarları kaydet

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
    """Yardım menüsü"""
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
    """Teknik analiz komutu"""
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

    # Yükleniyor mesajı
    loading_msg = await update.message.reply_text(f"🔄 {symbol} analiz ediliyor...")

    # Veri çek
    df = fetch_klines(symbol, interval)
    if df is None or df.empty:
        await loading_msg.edit_text("❌ Veri çekilemedi. Sembolü kontrol edin.")
        return

    ticker = fetch_ticker(symbol)

    # Analiz yap
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    result = perform_analysis(df, close, high, low, volume, settings)

    # Raporu formatla
    report = format_analysis_report(symbol, interval, result, ticker)

    # Analizi kaydet
    save_analysis(user_id, symbol, interval, result["overall"], 
                  result["entry"], result["sl_atr"], result["tp_atr"], result["rr_atr"])

    # Butonlar
    keyboard = [
        [InlineKeyboardButton("🔄 Yenile", callback_data=f"refresh:{symbol}:{interval}")],
        [InlineKeyboardButton("➕ İzleme Listesine Ekle", callback_data=f"addwatch:{symbol}")],
        [InlineKeyboardButton("🔔 Alarm Kur", callback_data=f"alert:{symbol}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await loading_msg.edit_text(report, parse_mode="HTML", reply_markup=reply_markup)

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anlık fiyat komutu"""
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
    """Alarm kurma komutu"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Kullanım: /alert &lt;sembol&gt; &lt;hedef_fiyat&gt;
"
            "Örnek: /alert BTCUSDT 70000
"
            "Örnek: /alert ETHUSDT 2000 (fiyat düşerse alarm)",
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

    # Mevcut fiyatı kontrol et
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
    """Aktif alarmları listele"""
    user_id = update.effective_user.id
    alerts = get_user_alerts(user_id)

    if not alerts:
        await update.message.reply_text("📭 Aktif alarmınız yok.
/alarm kurmak için: /alert BTCUSDT 70000")
        return

    text = "🔔 <b>AKTİF ALARMLARINIZ</b>

"
    for alert in alerts:
        alert_id, symbol, target, condition, created = alert
        direction = "📈 Yukarı" if condition == "above" else "📉 Aşağı"
        text += f"#{alert_id} | {symbol} | ${target:,.2f} | {direction}
"

    text += "
Silme: /removealert &lt;id&gt;"
    await update.message.reply_text(text, parse_mode="HTML")

async def removealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alarm silme"""
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
    """İzleme listesini göster"""
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
    """İzleme listesine ekle"""
    args = context.args
    if not args:
        await update.message.reply_text("❌ Kullanım: /addwatch &lt;sembol&gt;
Örnek: /addwatch BTCUSDT")
        return

    user_id = update.effective_user.id
    symbol = args[0].upper()

    # Sembolün geçerli olup olmadığını kontrol et
    ticker = fetch_ticker(symbol)
    if not ticker:
        await update.message.reply_text(f"❌ {symbol} bulunamadı.")
        return

    if add_to_watchlist(user_id, symbol):
        await update.message.reply_text(f"✅ {symbol} izleme listesine eklendi.")
    else:
        await update.message.reply_text(f"⚠️ {symbol} zaten listede.")

async def removewatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """İzleme listesinden çıkar"""
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
    """Ayarları göster"""
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
    """Bakiye ayarla"""
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
    """Risk yüzdesi ayarla"""
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
    """Kaldıraç ayarla"""
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
    """Inline buton callback'leri"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    if data.startswith("refresh:"):
        _, symbol, interval = data.split(":")

        # Analizi yenile
        df = fetch_klines(symbol, interval)
        if df is None:
            await query.edit_message_text("❌ Veri çekilemedi.")
            return

        ticker = fetch_ticker(symbol)
        result = perform_analysis(df, df["close"], df["high"], df["low"], df["volume"], settings)
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

        df = fetch_klines(symbol, interval)
        if df is None:
            await query.edit_message_text("❌ Veri çekilemedi.")
            return

        ticker = fetch_ticker(symbol)
        result = perform_analysis(df, df["close"], df["high"], df["low"], df["volume"], settings)
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
    """Düzenli olarak alarmları kontrol et"""
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
    """Botu başlat"""
    init_db()

    if TOKEN == "YOUR_BOT_TOKEN_HERE" or not TOKEN:
        print("❌ BOT_TOKEN çevre değişkeni ayarlanmamış!")
        print("export BOT_TOKEN=senin_bot_tokenin")
        print("export OWNER_ID=senin_telegram_id_n")
        return

    application = Application.builder().token(TOKEN).build()

    # Komut handlerları
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

    # Callback handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Alarm kontrolü (her 2 dakikada bir)
    application.job_queue.run_repeating(check_alerts, interval=120, first=10)

    print("🤖 Trade Analyzer Bot başlatılıyor...")
    print("💡 Komutlar: /start, /help, /analyze, /price, /alert")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
