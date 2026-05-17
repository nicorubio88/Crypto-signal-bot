"""
Motor de datos y cálculo de indicadores
Consume API pública de Binance — sin cuenta, sin riesgo
"""

import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime

# ── Configuración ────────────────────────────────────────────────────────────

SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}

BINANCE_URL = "https://api.binance.com/api/v3/klines"  # Spot (funciona sin restricciones geo)

# ── Fetch de velas ───────────────────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """
    Descarga velas de Binance y devuelve DataFrame limpio.
    interval: '4h' | '1d'
    limit: cantidad de velas (300 es suficiente para EMA200)
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_URL, params=params, timeout=10)
    resp.raise_for_status()

    raw = resp.json()
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


# ── Cálculo de indicadores ───────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega todos los indicadores al DataFrame.
    """
    c = df["close"]
    v = df["volume"]

    # Medias móviles simples
    df["MA20"]  = ta.sma(c, 20)
    df["MA50"]  = ta.sma(c, 50)
    df["MA200"] = ta.sma(c, 200)

    # Medias móviles exponenciales
    df["EMA20"]  = ta.ema(c, 20)
    df["EMA50"]  = ta.ema(c, 50)
    df["EMA200"] = ta.ema(c, 200)

    # Bollinger Bands (20, 2)
    bb = ta.bbands(c, length=20, std=2)
    df["BB_UP"] = bb["BBU_20_2.0"]
    df["BB_MB"] = bb["BBM_20_2.0"]
    df["BB_DN"] = bb["BBL_20_2.0"]

    # RSI múltiple
    df["RSI6"]  = ta.rsi(c, 6)
    df["RSI20"] = ta.rsi(c, 20)
    df["RSI50"] = ta.rsi(c, 50)

    # MACD (12, 26, 9)
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    df["MACD"]        = macd["MACD_12_26_9"]
    df["MACD_SIGNAL"] = macd["MACDs_12_26_9"]
    df["MACD_HIST"]   = macd["MACDh_12_26_9"]

    # Volumen MA10 para comparar
    df["VOL_MA10"] = ta.sma(v, 10)

    return df


# ── Sistema de scoring ───────────────────────────────────────────────────────

def calculate_score(row: pd.Series) -> dict:
    """
    Evalúa cada condición y devuelve score total + detalle.
    Score > 0 = sesgo alcista | Score < 0 = sesgo bajista
    """
    conditions = {}
    score = 0

    price = row["close"]

    # ── Capa 1: Tendencia ────────────────────────────────────────
    cond = price > row["EMA20"]
    conditions["Precio > EMA20"] = cond
    score += 1 if cond else -1

    cond = price > row["EMA50"]
    conditions["Precio > EMA50"] = cond
    score += 1 if cond else -1

    cond = price > row["BB_MB"]
    conditions["Precio > Bollinger MB"] = cond
    score += 1 if cond else -1

    # ── Capa 2: Momentum ─────────────────────────────────────────
    cond = row["MACD_HIST"] > 0
    conditions["MACD histograma positivo"] = cond
    score += 1 if cond else -1

    # Histograma creciendo (comparar con vela anterior no disponible en row único)
    # Lo manejamos en analyze() con acceso al df completo
    cond = row["MACD"] > 0 and row["MACD_SIGNAL"] > 0
    conditions["MACD líneas sobre cero"] = cond
    score += 1 if cond else -1

    cond = row["RSI20"] > 55
    conditions["RSI(20) > 55"] = cond
    score += 1 if cond else -1

    # RSI(6) sobreventa — señal especial de rebote
    cond_rsi_os = row["RSI6"] < 25
    conditions["RSI(6) < 25 (sobreventa)"] = cond_rsi_os
    # No suma al score de tendencia, es una alerta de timing

    # ── Capa 3: Volumen ──────────────────────────────────────────
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
    """
    Determina la señal final filtrando por tendencia diaria.
    """
    if score >= 4:
        raw = "LONG"
    elif score <= -4:
        raw = "SHORT"
    else:
        raw = "NEUTRAL"

    # Filtro de tendencia 1D — no operamos contra la marea
    if raw == "LONG" and trend_1d == "BAJISTA":
        return "NEUTRAL ⚠️ (contra tendencia 1D)"
    if raw == "SHORT" and trend_1d == "ALCISTA":
        return "NEUTRAL ⚠️ (contra tendencia 1D)"

    return raw


def get_trend_1d(row: pd.Series) -> str:
    """Determina tendencia del 1D basada en EMA20 y EMA50."""
    if row["close"] > row["EMA20"] and row["EMA20"] > row["EMA50"]:
        return "ALCISTA"
    elif row["close"] < row["EMA20"] and row["EMA20"] < row["EMA50"]:
        return "BAJISTA"
    else:
        return "LATERAL"


def get_levels(df: pd.DataFrame) -> dict:
    """Calcula stop loss y take profit sugeridos."""
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
    """
    Análisis completo de un activo.
    Devuelve dict con todo lo necesario para dashboard y Telegram.
    """
    try:
        # Fetch datos
        df_4h = fetch_candles(symbol, "4h", limit=300)
        df_1d = fetch_candles(symbol, "1d", limit=300)

        # Calcular indicadores
        df_4h = calculate_indicators(df_4h)
        df_1d = calculate_indicators(df_1d)

        # Última vela completa (penúltima fila — la última puede estar incompleta)
        last_4h = df_4h.iloc[-2]
        last_1d = df_1d.iloc[-2]

        # Scoring en 4H
        score_data = calculate_score(last_4h)

        # Histograma MACD creciendo (comparación con vela anterior)
        macd_growing = df_4h["MACD_HIST"].iloc[-2] > df_4h["MACD_HIST"].iloc[-3]
        score_data["conditions"]["MACD histograma creciendo"] = macd_growing

        # Tendencia 1D
        trend_1d = get_trend_1d(last_1d)

        # Señal final
        signal = get_signal(score_data["score"], trend_1d)

        # Niveles
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
    """Corre el análisis para todos los activos configurados."""
    results = []
    for name, symbol in SYMBOLS.items():
        print(f"  Analizando {name}...")
        results.append(analyze(name, symbol))
    return results


if __name__ == "__main__":
    print("🔍 Corriendo análisis de prueba...")
    results = run_analysis()
    for r in results:
        if r["error"]:
            print(f"\n❌ {r['name']}: {r['error']}")
        else:
            print(f"\n{'='*50}")
            print(f"{r['name']} | ${r['price']} | Score: {r['score']}/{r['max_score']} | {r['signal']}")
            print(f"Tendencia 1D: {r['trend_1d']}")
            print(f"RSI(6): {r['rsi6']} | RSI(20): {r['rsi20']}")
            print(f"MACD hist: {r['macd_hist']}")
            for cond, val in r['conditions'].items():
                icon = "✅" if val else "❌"
                print(f"  {icon} {cond}")
