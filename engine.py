"""
Motor de datos y cálculo de indicadores
Fuente: Kraken API pública (sin cuenta, sin límites, sin geo-restricciones)
"""

import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime

# ── Configuración ────────────────────────────────────────────────────────────

SYMBOLS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
}

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"4h": 240, "1d": 1440}

# ── Fetch de velas desde Kraken ──────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """
    Descarga velas de Kraken.
    interval: '4h' | '1d'
    """
    params = {"pair": symbol, "interval": KRAKEN_INTERVALS[interval]}
    resp = requests.get(KRAKEN_URL, params=params, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")

    result = data["result"]
    pair_key = [k for k in result.keys() if k != "last"][0]
    raw = result[pair_key]

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="s")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df.set_index("open_time", inplace=True)
    df = df.tail(limit)

    return df[["open", "high", "low", "close", "volume"]]


# ── Cálculo de indicadores ───────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    v = df["volume"]

    df["MA20"]  = ta.sma(c, 20)
    df["MA50"]  = ta.sma(c, 50)
    df["MA200"] = ta.sma(c, 200)
    df["EMA20"]  = ta.ema(c, 20)
    df["EMA50"]  = ta.ema(c, 50)
    df["EMA200"] = ta.ema(c, 200)

    # Bollinger Bands — detectar nombre de columna dinámicamente
    bb = ta.bbands(c, length=20, std=2)
    bb_up_col = [col for col in bb.columns if col.startswith("BBU")][0]
    bb_mb_col = [col for col in bb.columns if col.startswith("BBM")][0]
    bb_dn_col = [col for col in bb.columns if col.startswith("BBL")][0]
    df["BB_UP"] = bb[bb_up_col]
    df["BB_MB"] = bb[bb_mb_col]
    df["BB_DN"] = bb[bb_dn_col]

    df["RSI6"]  = ta.rsi(c, 6)
    df["RSI20"] = ta.rsi(c, 20)
    df["RSI50"] = ta.rsi(c, 50)

    macd = ta.macd(c, fast=12, slow=26, signal=9)
    macd_col     = [col for col in macd.columns if col.startswith("MACD_")][0]
    macds_col    = [col for col in macd.columns if col.startswith("MACDs")][0]
    macdh_col    = [col for col in macd.columns if col.startswith("MACDh")][0]
    df["MACD"]        = macd[macd_col]
    df["MACD_SIGNAL"] = macd[macds_col]
    df["MACD_HIST"]   = macd[macdh_col]

    df["VOL_MA10"] = ta.sma(v, 10)

    return df


# ── Sistema de scoring ───────────────────────────────────────────────────────

def calculate_score(row: pd.Series) -> dict:
    conditions = {}
    score = 0
    price = row["close"]

    cond = price > row["EMA20"]
    conditions["Precio > EMA20"] = cond
    score += 1 if cond else -1

    cond = price > row["EMA50"]
    conditions["Precio > EMA50"] = cond
    score += 1 if cond else -1

    cond = price > row["BB_MB"]
    conditions["Precio > Bollinger MB"] = cond
    score += 1 if cond else -1

    cond = row["MACD_HIST"] > 0
    conditions["MACD histograma positivo"] = cond
    score += 1 if cond else -1

    cond = row["MACD"] > 0 and row["MACD_SIGNAL"] > 0
    conditions["MACD lineas sobre cero"] = cond
    score += 1 if cond else -1

    cond = row["RSI20"] > 55
    conditions["RSI(20) > 55"] = cond
    score += 1 if cond else -1

    cond_rsi_os = row["RSI6"] < 25
    conditions["RSI(6) < 25 (sobreventa)"] = cond_rsi_os

    cond = row["volume"] > row["VOL_MA10"]
    conditions["Volumen > MA10"] = cond
    score += 1 if cond else -1

    return {
        "score": score,
        "max_score": 7,
        "conditions": conditions,
        "rsi6_oversold": cond_rsi_os,
        "rsi6_overbought": row["RSI6"] > 75,
    }


def get_signal(score: int, trend_1d: str) -> str:
    if score >= 4:
        raw = "LONG"
    elif score <= -4:
        raw = "SHORT"
    else:
        raw = "NEUTRAL"

    if raw == "LONG" and trend_1d == "BAJISTA":
        return "NEUTRAL (contra tendencia 1D)"
    if raw == "SHORT" and trend_1d == "ALCISTA":
        return "NEUTRAL (contra tendencia 1D)"
    return raw


def get_trend_1d(row: pd.Series) -> str:
    if row["close"] > row["EMA20"] and row["EMA20"] > row["EMA50"]:
        return "ALCISTA"
    elif row["close"] < row["EMA20"] and row["EMA20"] < row["EMA50"]:
        return "BAJISTA"
    else:
        return "LATERAL"


def get_levels(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    price = last["close"]
    atr = ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1]
    return {
        "stop_long":  round(price - 2 * atr, 4),
        "tp1_long":   round(price + 2 * atr, 4),
        "tp2_long":   round(price + 4 * atr, 4),
        "stop_short": round(price + 2 * atr, 4),
        "tp1_short":  round(price - 2 * atr, 4),
        "tp2_short":  round(price - 4 * atr, 4),
        "atr":        round(atr, 4),
    }


# ── Análisis completo por símbolo ────────────────────────────────────────────

def analyze(name: str, symbol: str) -> dict:
    try:
        df_4h = fetch_candles(symbol, "4h", limit=300)
        time.sleep(1)  # pausa entre requests
        df_1d = fetch_candles(symbol, "1d", limit=300)

        df_4h = calculate_indicators(df_4h)
        df_1d = calculate_indicators(df_1d)

        last_4h = df_4h.iloc[-2]
        last_1d = df_1d.iloc[-2]

        score_data = calculate_score(last_4h)

        macd_growing = df_4h["MACD_HIST"].iloc[-2] > df_4h["MACD_HIST"].iloc[-3]
        score_data["conditions"]["MACD histograma creciendo"] = macd_growing

        trend_1d = get_trend_1d(last_1d)
        signal = get_signal(score_data["score"], trend_1d)
        levels = get_levels(df_4h)

        return {
            "name": name,
            "symbol": symbol,
            "price": round(last_4h["close"], 4),
            "score": score_data["score"],
            "max_score": score_data["max_score"],
            "signal": signal,
            "trend_1d": trend_1d,
            "conditions": score_data["conditions"],
            "rsi6": round(last_4h["RSI6"], 1),
            "rsi20": round(last_4h["RSI20"], 1),
            "rsi6_oversold": score_data["rsi6_oversold"],
            "rsi6_overbought": score_data["rsi6_overbought"],
            "macd": round(last_4h["MACD"], 4),
            "macd_hist": round(last_4h["MACD_HIST"], 4),
            "ema20": round(last_4h["EMA20"], 4),
            "ema50": round(last_4h["EMA50"], 4),
            "bb_up": round(last_4h["BB_UP"], 4),
            "bb_mb": round(last_4h["BB_MB"], 4),
            "bb_dn": round(last_4h["BB_DN"], 4),
            "levels": levels,
            "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "error": None,
        }

    except Exception as e:
        return {"name": name, "symbol": symbol, "error": str(e)}


def run_analysis() -> list:
    results = []
    for name, symbol in SYMBOLS.items():
        print(f"  Analizando {name}...")
        results.append(analyze(name, symbol))
        time.sleep(1)
    return results


if __name__ == "__main__":
    print("Corriendo analisis de prueba...")
    results = run_analysis()
    for r in results:
        if r["error"]:
            print(f"\n ERROR {r['name']}: {r['error']}")
        else:
            print(f"\n{'='*50}")
            print(f"{r['name']} | ${r['price']} | Score: {r['score']}/{r['max_score']} | {r['signal']}")
            print(f"Tendencia 1D: {r['trend_1d']}")
            print(f"RSI(6): {r['rsi6']} | RSI(20): {r['rsi20']}")
            print(f"MACD hist: {r['macd_hist']}")
            for cond, val in r['conditions'].items():
                icon = "OK" if val else "NO"
                print(f"  [{icon}] {cond}")
