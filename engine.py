"""
Motor de datos y calculo de indicadores v2.1
Fuente: Kraken API publica

Capas de analisis:
  1. Filtro macro 1W (tendencia semanal)
  2. Filtro tendencia 1D
  3. Setup de entrada 4H (scoring principal)
  4. Confirmacion 1H (timing)

Indicadores: EMA20/50/200, Bollinger Bands + Width, RSI (6/14/20),
StochRSI, MACD, ADX + DI, OBV, VWAP diario, ATR, divergencias RSI, pivots.
"""

import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime

# ── Configuracion ─────────────────────────────────────────────────────────────

SYMBOLS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
}

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_INTERVALS = {"1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

# Score maximo teorico real (suma de todos los pesos positivos)
# EMA: 3 | Bollinger MB: 1 | VWAP: 1 | MACD: 2 | StochRSI: 1 |
# RSI20: 1 | Divergencia RSI: 3 | OBV: 1 | Divergencia OBV: 2 | Volumen: 1 = 16
MAX_SCORE = 16
SIGNAL_THRESHOLD = 9  # ajustado proporcionalmente al nuevo max


# ── Fetch de velas ────────────────────────────────────────────────────────────

def fetch_candles(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
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
        "open_time", "open", "high", "low", "close", "vwap_raw", "volume", "count"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="s")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df[["open", "high", "low", "close", "volume"]].tail(limit)


# ── Calculo de indicadores ────────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # Medias moviles
    df["MA20"]   = ta.sma(c, 20)
    df["MA50"]   = ta.sma(c, 50)
    df["MA200"]  = ta.sma(c, 200)
    df["EMA20"]  = ta.ema(c, 20)
    df["EMA50"]  = ta.ema(c, 50)
    df["EMA200"] = ta.ema(c, 200)

    # Bollinger Bands + BB Width
    bb = ta.bbands(c, length=20, std=2)
    bb_up_col = [col for col in bb.columns if col.startswith("BBU")][0]
    bb_mb_col = [col for col in bb.columns if col.startswith("BBM")][0]
    bb_dn_col = [col for col in bb.columns if col.startswith("BBL")][0]
    df["BB_UP"] = bb[bb_up_col]
    df["BB_MB"] = bb[bb_mb_col]
    df["BB_DN"] = bb[bb_dn_col]
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_DN"]) / df["BB_MB"]

    # RSI multiple
    df["RSI6"]  = ta.rsi(c, 6)
    df["RSI14"] = ta.rsi(c, 14)
    df["RSI20"] = ta.rsi(c, 20)

    # Stochastic RSI (3,3,14,14)
    try:
        stoch = ta.stochrsi(c, length=14, rsi_length=14, k=3, d=3)
        if stoch is not None and not stoch.empty:
            k_col = [col for col in stoch.columns if "_K" in col]
            d_col = [col for col in stoch.columns if "_D" in col]
            if k_col: df["STOCH_K"] = stoch[k_col[0]]
            if d_col: df["STOCH_D"] = stoch[d_col[0]]
    except Exception:
        pass

    # MACD (12,26,9)
    macd = ta.macd(c, fast=12, slow=26, signal=9)
    macd_col  = [col for col in macd.columns if col.startswith("MACD_")][0]
    macds_col = [col for col in macd.columns if col.startswith("MACDs")][0]
    macdh_col = [col for col in macd.columns if col.startswith("MACDh")][0]
    df["MACD"]        = macd[macd_col]
    df["MACD_SIGNAL"] = macd[macds_col]
    df["MACD_HIST"]   = macd[macdh_col]

    # ADX (14)
    adx = ta.adx(h, l, c, length=14)
    adx_col = [col for col in adx.columns if col.startswith("ADX_")][0]
    dmp_col = [col for col in adx.columns if col.startswith("DMP_")][0]
    dmn_col = [col for col in adx.columns if col.startswith("DMN_")][0]
    df["ADX"]    = adx[adx_col]
    df["DI_POS"] = adx[dmp_col]
    df["DI_NEG"] = adx[dmn_col]

    # OBV
    df["OBV"]      = ta.obv(c, v)
    df["OBV_MA20"] = ta.sma(df["OBV"], 20)

    # ATR (14)
    df["ATR"] = ta.atr(h, l, c, length=14)

    # Volumen MA
    df["VOL_MA10"] = ta.sma(v, 10)

    return df


def add_daily_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula VWAP que se resetea por dia (estandar institucional).
    Usa typical price = (H+L+C)/3.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.normalize()
    df["VWAP"] = pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
    return df


# ── Divergencias RSI (correcta: minimos secuenciales) ─────────────────────────

def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Detecta divergencias usando dos minimos/maximos secuenciales reales.
    Logica:
      - Busca el minimo mas reciente en la primera mitad del lookback
      - Busca el minimo mas reciente en la segunda mitad
      - Compara cronologicamente: minimo viejo vs minimo nuevo

    Divergencia alcista: precio hace minimo mas bajo, RSI mas alto
    Divergencia bajista: precio hace maximo mas alto, RSI mas bajo
    """
    if len(df) < lookback + 2 or "RSI14" not in df.columns:
        return {"bullish": False, "bearish": False}

    recent = df.tail(lookback).dropna(subset=["RSI14"])
    if len(recent) < 10:
        return {"bullish": False, "bearish": False}

    # Dividir en dos mitades cronologicas
    mid       = len(recent) // 2
    first_half  = recent.iloc[:mid]
    second_half = recent.iloc[mid:]

    # ── Divergencia alcista: dos minimos ──────────────────────────────────────
    p_low_idx_1 = first_half["close"].idxmin()
    p_low_idx_2 = second_half["close"].idxmin()
    p_low_1 = first_half.loc[p_low_idx_1, "close"]
    p_low_2 = second_half.loc[p_low_idx_2, "close"]
    rsi_low_1 = first_half.loc[p_low_idx_1, "RSI14"]
    rsi_low_2 = second_half.loc[p_low_idx_2, "RSI14"]

    bullish = (
        p_low_2 < p_low_1            # precio segundo minimo mas bajo
        and rsi_low_2 > rsi_low_1 + 3 # RSI segundo minimo mas alto (margen 3 puntos)
        and rsi_low_1 < 40            # primer minimo en zona de sobreventa
    )

    # ── Divergencia bajista: dos maximos ──────────────────────────────────────
    p_high_idx_1 = first_half["close"].idxmax()
    p_high_idx_2 = second_half["close"].idxmax()
    p_high_1 = first_half.loc[p_high_idx_1, "close"]
    p_high_2 = second_half.loc[p_high_idx_2, "close"]
    rsi_high_1 = first_half.loc[p_high_idx_1, "RSI14"]
    rsi_high_2 = second_half.loc[p_high_idx_2, "RSI14"]

    bearish = (
        p_high_2 > p_high_1
        and rsi_high_2 < rsi_high_1 - 3
        and rsi_high_1 > 60           # primer maximo en zona de sobrecompra
    )

    return {"bullish": bool(bullish), "bearish": bool(bearish)}


# ── Divergencias OBV (precio vs volumen acumulado) ───────────────────────────

def detect_obv_divergence(df: pd.DataFrame, lookback: int = 30) -> dict:
    """
    Detecta divergencias entre precio y OBV.

    Divergencia alcista OBV: precio hace minimo mas bajo, OBV hace minimo mas alto
      → grandes manos compran mientras retail vende = posible reversion alcista

    Divergencia bajista OBV: precio hace maximo mas alto, OBV hace maximo mas bajo
      → grandes manos venden mientras retail compra = posible reversion bajista

    Mas confiable que divergencia RSI porque mide presion REAL de volumen,
    no solo momentum del precio.
    """
    if len(df) < lookback + 2 or "OBV" not in df.columns:
        return {"bullish": False, "bearish": False}

    recent = df.tail(lookback).dropna(subset=["OBV"])
    if len(recent) < 10:
        return {"bullish": False, "bearish": False}

    mid = len(recent) // 2
    first_half  = recent.iloc[:mid]
    second_half = recent.iloc[mid:]

    # ── Divergencia alcista ──────────────────────────────────────────────────
    p_low_idx_1 = first_half["close"].idxmin()
    p_low_idx_2 = second_half["close"].idxmin()
    p_low_1 = first_half.loc[p_low_idx_1, "close"]
    p_low_2 = second_half.loc[p_low_idx_2, "close"]
    obv_low_1 = first_half.loc[p_low_idx_1, "OBV"]
    obv_low_2 = second_half.loc[p_low_idx_2, "OBV"]

    # Margen de 0.5% para precio (significativo) y OBV creciendo (acumulacion)
    bullish = (
        p_low_2 < p_low_1 * 0.995
        and obv_low_2 > obv_low_1
    )

    # ── Divergencia bajista ──────────────────────────────────────────────────
    p_high_idx_1 = first_half["close"].idxmax()
    p_high_idx_2 = second_half["close"].idxmax()
    p_high_1 = first_half.loc[p_high_idx_1, "close"]
    p_high_2 = second_half.loc[p_high_idx_2, "close"]
    obv_high_1 = first_half.loc[p_high_idx_1, "OBV"]
    obv_high_2 = second_half.loc[p_high_idx_2, "OBV"]

    bearish = (
        p_high_2 > p_high_1 * 1.005
        and obv_high_2 < obv_high_1
    )

    return {"bullish": bool(bullish), "bearish": bool(bearish)}


# ── Pivots classicos (sobre vela anterior) ───────────────────────────────────

def calculate_pivots(df: pd.DataFrame) -> dict:
    """
    Pivots clasicos usando vela anterior cerrada (vela -2, ya que -1 puede estar activa).
    Estandar: pivot = (H+L+C)/3 de la vela previa.
    """
    if len(df) < 3:
        return {}
    prev = df.iloc[-3]  # vela cerrada previa a la ultima cerrada
    pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
    rng   = prev["high"] - prev["low"]
    return {
        "pivot": round(pivot, 4),
        "r1": round(2 * pivot - prev["low"], 4),
        "r2": round(pivot + rng, 4),
        "s1": round(2 * pivot - prev["high"], 4),
        "s2": round(pivot - rng, 4),
    }


# ── Tendencia por timeframe ──────────────────────────────────────────────────

def get_trend(row: pd.Series) -> str:
    """
    Tendencia basada en alineacion de 3 EMAs (20, 50, 200).

    Estandar de la industria para evitar 'bear/bull traps':
    - ALCISTA: precio > EMA20 > EMA50 > EMA200 (alineacion completa)
    - BAJISTA: precio < EMA20 < EMA50 < EMA200 (alineacion completa)
    - LATERAL: cualquier desalineacion

    Para timeframes con pocos datos (1W con < 200 velas), usa EMA50 como fallback.
    """
    if pd.isna(row.get("EMA20")) or pd.isna(row.get("EMA50")):
        return "LATERAL"

    price = row["close"]
    ema20 = row["EMA20"]
    ema50 = row["EMA50"]
    ema200 = row.get("EMA200")

    # Si EMA200 no esta disponible (pocas velas), usar logica de 2 EMAs
    if pd.isna(ema200):
        if price > ema20 and ema20 > ema50:
            return "ALCISTA"
        if price < ema20 and ema20 < ema50:
            return "BAJISTA"
        return "LATERAL"

    # Logica completa con 3 EMAs (mas precisa)
    if price > ema20 and ema20 > ema50 and ema50 > ema200:
        return "ALCISTA"
    if price < ema20 and ema20 < ema50 and ema50 < ema200:
        return "BAJISTA"
    return "LATERAL"


# ── Sistema de scoring ponderado ──────────────────────────────────────────────

def calculate_score(df: pd.DataFrame) -> dict:
    """
    Score ponderado v2.2 (max 16 puntos).
    ADX < 25 = filtro obligatorio, no emite señal.
    """
    if len(df) < 3:
        return {"score": 0, "max_score": MAX_SCORE, "conditions": {},
                "market_trending": False, "adx": None, "di_pos": None, "di_neg": None,
                "rsi6_oversold": False, "rsi6_overbought": False,
                "bb_squeeze": False,
                "divergence": {"bullish": False, "bearish": False},
                "obv_divergence": {"bullish": False, "bearish": False},
                "warnings": ["Datos insuficientes"]}

    row      = df.iloc[-2]   # ultima vela CERRADA
    prev_row = df.iloc[-3]
    price    = row["close"]

    conditions = {}
    score = 0
    warnings = []

    # ── FILTRO OBLIGATORIO: ADX ───────────────────────────────────────────────
    adx_value = row.get("ADX")
    market_trending = bool(adx_value >= 25) if pd.notna(adx_value) else False
    conditions["ADX > 25 (mercado en tendencia)"] = market_trending

    if not market_trending:
        warnings.append(
            f"ADX {round(adx_value,1) if pd.notna(adx_value) else '?'} < 25 "
            "— mercado lateral, señal poco confiable"
        )

    # ── CAPA 1: TENDENCIA (max 5 puntos) ─────────────────────────────────────

    # EMA cruce 20/50 — 3 puntos (mas confiable)
    ema_bull = price > row["EMA20"] and row["EMA20"] > row["EMA50"]
    ema_bear = price < row["EMA20"] and row["EMA20"] < row["EMA50"]
    conditions["EMA 20/50 alineadas (alcista)"] = ema_bull
    conditions["EMA 20/50 alineadas (bajista)"] = ema_bear
    score += 3 if ema_bull else -3 if ema_bear else 0

    # Bollinger MB — 1 punto
    bb_above = price > row["BB_MB"]
    conditions["Precio > Bollinger MB"] = bb_above
    score += 1 if bb_above else -1

    # VWAP diario — 1 punto
    if pd.notna(row.get("VWAP")):
        vwap_above = price > row["VWAP"]
        conditions["Precio > VWAP diario"] = vwap_above
        score += 1 if vwap_above else -1

    # BB Width — informativo, no suma puntos
    bb_squeeze = False
    if pd.notna(row.get("BB_WIDTH")):
        recent_widths = df["BB_WIDTH"].tail(100).dropna()
        if len(recent_widths) > 20:
            p20 = recent_widths.quantile(0.20)
            bb_squeeze = row["BB_WIDTH"] < p20
    conditions["Bollinger Squeeze (señal proxima)"] = bb_squeeze

    # ── CAPA 2: MOMENTUM (max 7 puntos) ──────────────────────────────────────

    # MACD: posicion + aceleracion — 2 puntos
    macd_bull = (
        row["MACD"] > row["MACD_SIGNAL"]
        and row["MACD_HIST"] > prev_row["MACD_HIST"]
    )
    macd_bear = (
        row["MACD"] < row["MACD_SIGNAL"]
        and row["MACD_HIST"] < prev_row["MACD_HIST"]
    )
    conditions["MACD bullish (linea > señal + acelerando)"] = macd_bull
    conditions["MACD bearish (linea < señal + acelerando)"] = macd_bear
    score += 2 if macd_bull else -2 if macd_bear else 0

    # StochRSI — 1 punto
    stoch_bull = False
    stoch_bear = False
    if pd.notna(row.get("STOCH_K")) and pd.notna(row.get("STOCH_D")):
        stoch_bull = row["STOCH_K"] > row["STOCH_D"] and row["STOCH_K"] < 80
        stoch_bear = row["STOCH_K"] < row["STOCH_D"] and row["STOCH_K"] > 20
    conditions["StochRSI alcista (K>D, no sobrecompra)"] = stoch_bull
    conditions["StochRSI bajista (K<D, no sobreventa)"] = stoch_bear
    score += 1 if stoch_bull else -1 if stoch_bear else 0

    # RSI20 nivel — 1 punto
    rsi20_bull = pd.notna(row["RSI20"]) and row["RSI20"] > 55
    rsi20_bear = pd.notna(row["RSI20"]) and row["RSI20"] < 45
    conditions["RSI(20) > 55"] = rsi20_bull
    conditions["RSI(20) < 45"] = rsi20_bear
    score += 1 if rsi20_bull else -1 if rsi20_bear else 0

    # Divergencias RSI — 3 puntos (muy alta confiabilidad)
    div = detect_rsi_divergence(df)
    conditions["Divergencia RSI alcista"] = div["bullish"]
    conditions["Divergencia RSI bajista"] = div["bearish"]
    if div["bullish"]:
        score += 3
    elif div["bearish"]:
        score -= 3

    # ── CAPA 3: VOLUMEN (max 4 puntos) ────────────────────────────────────────

    # OBV vs MA20 — 1 punto
    obv_bull = False
    if pd.notna(row.get("OBV_MA20")):
        obv_bull = row["OBV"] > row["OBV_MA20"]
    conditions["OBV > MA20 (presion compradora)"] = obv_bull
    score += 1 if obv_bull else -1

    # Divergencias OBV — 2 puntos (mas confiable que RSI por usar volumen real)
    obv_div = detect_obv_divergence(df)
    conditions["Divergencia OBV alcista (acumulacion)"] = obv_div["bullish"]
    conditions["Divergencia OBV bajista (distribucion)"] = obv_div["bearish"]
    if obv_div["bullish"]:
        score += 2
    elif obv_div["bearish"]:
        score -= 2

    # Volumen fuerte — 1 punto (solo premia, no penaliza)
    vol_strong = False
    if pd.notna(row.get("VOL_MA10")):
        vol_strong = row["volume"] > row["VOL_MA10"] * 1.5
    conditions["Volumen fuerte (> MA10 x 1.5)"] = vol_strong
    score += 1 if vol_strong else 0

    # RSI6 alertas especiales (no suman puntos)
    rsi6_oversold   = pd.notna(row["RSI6"]) and row["RSI6"] < 25
    rsi6_overbought = pd.notna(row["RSI6"]) and row["RSI6"] > 75

    return {
        "score": score,
        "max_score": MAX_SCORE,
        "conditions": conditions,
        "market_trending": market_trending,
        "adx": round(float(adx_value), 1) if pd.notna(adx_value) else None,
        "di_pos": round(float(row["DI_POS"]), 1) if pd.notna(row.get("DI_POS")) else None,
        "di_neg": round(float(row["DI_NEG"]), 1) if pd.notna(row.get("DI_NEG")) else None,
        "rsi6_oversold": rsi6_oversold,
        "rsi6_overbought": rsi6_overbought,
        "bb_squeeze": bool(bb_squeeze),
        "divergence": div,
        "obv_divergence": obv_div,
        "warnings": warnings,
    }


# ── Señal final (incluye confirmacion 1H y filtros multi-timeframe) ──────────

def get_signal(score: int, trend_1h: str, trend_1d: str, trend_1w: str,
               market_trending: bool) -> dict:
    """
    Logica de señal con 4 filtros: ADX, 1W, 1D, 1H.
    """
    reasons = []

    # Filtro 1: mercado en tendencia
    if not market_trending:
        return {"signal": "NEUTRAL",
                "reason": "Mercado lateral (ADX < 25)",
                "confidence": "—"}

    # Direccion segun score
    if score >= SIGNAL_THRESHOLD:
        raw = "LONG"
    elif score <= -SIGNAL_THRESHOLD:
        raw = "SHORT"
    else:
        return {"signal": "NEUTRAL",
                "reason": f"Score insuficiente ({score}, umbral {SIGNAL_THRESHOLD})",
                "confidence": "—"}

    # Filtro 2: tendencia 1W
    if raw == "LONG" and trend_1w == "BAJISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia semanal (BAJISTA)",
                "confidence": "—"}
    if raw == "SHORT" and trend_1w == "ALCISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia semanal (ALCISTA)",
                "confidence": "—"}

    # Filtro 3: tendencia 1D
    if raw == "LONG" and trend_1d == "BAJISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia diaria",
                "confidence": "—"}
    if raw == "SHORT" and trend_1d == "ALCISTA":
        return {"signal": "NEUTRAL",
                "reason": "Contra tendencia diaria",
                "confidence": "—"}

    # Filtro 4: confirmacion 1H
    target = "ALCISTA" if raw == "LONG" else "BAJISTA"
    confirms_1h = trend_1h == target

    # Calculo de confianza
    abs_score = abs(score)
    aligned_1d_1w = (trend_1d == trend_1w) and trend_1d in ("ALCISTA", "BAJISTA")

    if abs_score >= 10 and aligned_1d_1w and confirms_1h:
        confidence = "ALTA"
    elif abs_score >= 8 and confirms_1h:
        confidence = "MEDIA"
    elif abs_score >= 8:
        confidence = "BAJA"
        reasons.append("1H no confirma — esperar timing")
    else:
        confidence = "BAJA"

    return {"signal": raw,
            "reason": " | ".join(reasons) if reasons else "",
            "confidence": confidence}


# ── Señal de cierre (logica de ESTADO, no de cruce momentaneo) ───────────────

def get_close_signal(df: pd.DataFrame, open_signal: str) -> dict:
    """
    Detecta condiciones de cierre evaluando ESTADO ACTUAL.
    No requiere cruces exactos en la vela actual.
    """
    if len(df) < 3:
        return {"should_close": False, "urgency": "BAJA", "reasons": []}

    row   = df.iloc[-2]
    price = row["close"]
    reasons = []

    if open_signal == "LONG":
        # MACD bajo señal — estado bajista
        if pd.notna(row["MACD"]) and pd.notna(row["MACD_SIGNAL"]):
            if row["MACD"] < row["MACD_SIGNAL"]:
                reasons.append("MACD por debajo de su señal")

        # Precio bajo EMA20
        if pd.notna(row["EMA20"]) and price < row["EMA20"]:
            reasons.append("Precio por debajo de EMA20")

        # Sobrecompra extrema = momento de tomar ganancia
        if pd.notna(row["RSI6"]) and row["RSI6"] > 75:
            reasons.append(f"RSI(6) sobrecompra ({round(row['RSI6'],1)})")

        # ADX confirma direccion contraria
        if (pd.notna(row.get("ADX")) and pd.notna(row.get("DI_NEG"))
                and pd.notna(row.get("DI_POS"))):
            if row["ADX"] > 25 and row["DI_NEG"] > row["DI_POS"]:
                reasons.append("ADX confirma fuerza bajista")

    elif open_signal == "SHORT":
        if pd.notna(row["MACD"]) and pd.notna(row["MACD_SIGNAL"]):
            if row["MACD"] > row["MACD_SIGNAL"]:
                reasons.append("MACD por encima de su señal")

        if pd.notna(row["EMA20"]) and price > row["EMA20"]:
            reasons.append("Precio por encima de EMA20")

        if pd.notna(row["RSI6"]) and row["RSI6"] < 25:
            reasons.append(f"RSI(6) sobreventa ({round(row['RSI6'],1)})")

        if (pd.notna(row.get("ADX")) and pd.notna(row.get("DI_NEG"))
                and pd.notna(row.get("DI_POS"))):
            if row["ADX"] > 25 and row["DI_POS"] > row["DI_NEG"]:
                reasons.append("ADX confirma fuerza alcista")

    n = len(reasons)
    should_close = n >= 2
    urgency = "ALTA" if n >= 3 else "MEDIA" if n == 2 else "BAJA"

    return {"should_close": should_close, "urgency": urgency, "reasons": reasons}


# ── Niveles ATR ──────────────────────────────────────────────────────────────

def get_levels(df: pd.DataFrame) -> dict:
    """Stops y TPs escalonados (perfil medio-agresivo)."""
    last  = df.iloc[-2]
    price = last["close"]
    atr   = last["ATR"]

    sl_mult, tp1_mult, tp2_mult, tp3_mult = 1.5, 2.5, 4.0, 6.0

    return {
        "stop_long":  round(price - sl_mult * atr, 4),
        "tp1_long":   round(price + tp1_mult * atr, 4),
        "tp2_long":   round(price + tp2_mult * atr, 4),
        "tp3_long":   round(price + tp3_mult * atr, 4),
        "stop_short": round(price + sl_mult * atr, 4),
        "tp1_short":  round(price - tp1_mult * atr, 4),
        "tp2_short":  round(price - tp2_mult * atr, 4),
        "tp3_short":  round(price - tp3_mult * atr, 4),
        "atr":        round(float(atr), 4),
        "rr_ratio":   round(tp1_mult / sl_mult, 2),
    }


def calc_position_size(price: float, stop: float, capital: float = 10000,
                       risk_pct: float = 0.025) -> dict:
    """Tamaño de posicion para riesgo de 2.5% del capital."""
    risk_amount   = capital * risk_pct
    stop_distance = abs(price - stop) / price
    position_size = risk_amount / stop_distance if stop_distance > 0 else 0
    contracts     = position_size / price if price > 0 else 0
    return {
        "risk_amount":   round(risk_amount, 2),
        "stop_distance": round(stop_distance * 100, 2),
        "position_usd":  round(position_size, 2),
        "contracts":     round(contracts, 4),
    }


# ── Analisis completo ─────────────────────────────────────────────────────────

def analyze(name: str, symbol: str) -> dict:
    try:
        df_1h = fetch_candles(symbol, "1h", limit=200)
        time.sleep(0.5)
        df_4h = fetch_candles(symbol, "4h", limit=300)
        time.sleep(0.5)
        df_1d = fetch_candles(symbol, "1d", limit=300)
        time.sleep(0.5)
        df_1w = fetch_candles(symbol, "1w", limit=100)

        df_1h = calculate_indicators(df_1h)
        df_4h = calculate_indicators(df_4h)
        df_1d = calculate_indicators(df_1d)
        df_1w = calculate_indicators(df_1w)

        # VWAP solo tiene sentido en 4H y menores
        df_4h = add_daily_vwap(df_4h)
        df_1h = add_daily_vwap(df_1h)

        trend_1h = get_trend(df_1h.iloc[-2])
        trend_1d = get_trend(df_1d.iloc[-2])
        trend_1w = get_trend(df_1w.iloc[-2])

        score_data = calculate_score(df_4h)
        signal_data = get_signal(
            score_data["score"], trend_1h, trend_1d, trend_1w,
            score_data["market_trending"]
        )

        levels = get_levels(df_4h)
        pivots = calculate_pivots(df_4h)
        last   = df_4h.iloc[-2]
        price  = round(float(last["close"]), 4)

        close_long  = get_close_signal(df_4h, "LONG")
        close_short = get_close_signal(df_4h, "SHORT")

        target_for_signal = "ALCISTA" if "LONG" in signal_data["signal"] else "BAJISTA" if "SHORT" in signal_data["signal"] else None
        confirm_1h = (trend_1h == target_for_signal) if target_for_signal else False

        return {
            "name": name,
            "symbol": symbol,
            "price": price,
            "score": score_data["score"],
            "max_score": score_data["max_score"],
            "signal": signal_data["signal"],
            "signal_reason": signal_data["reason"],
            "confidence": signal_data["confidence"],
            "trend_1h": trend_1h,
            "trend_1d": trend_1d,
            "trend_1w": trend_1w,
            "confirm_1h": confirm_1h,
            "market_trending": score_data["market_trending"],
            "adx": score_data["adx"],
            "di_pos": score_data["di_pos"],
            "di_neg": score_data["di_neg"],
            "conditions": score_data["conditions"],
            "divergence": score_data["divergence"],
            "obv_divergence": score_data["obv_divergence"],
            "bb_squeeze": score_data["bb_squeeze"],
            "warnings": score_data["warnings"],
            "rsi6":  round(float(last["RSI6"]), 1)  if pd.notna(last["RSI6"])  else None,
            "rsi14": round(float(last["RSI14"]), 1) if pd.notna(last["RSI14"]) else None,
            "rsi20": round(float(last["RSI20"]), 1) if pd.notna(last["RSI20"]) else None,
            "rsi6_oversold": score_data["rsi6_oversold"],
            "rsi6_overbought": score_data["rsi6_overbought"],
            "macd":      round(float(last["MACD"]), 4)      if pd.notna(last["MACD"]) else None,
            "macd_hist": round(float(last["MACD_HIST"]), 4) if pd.notna(last["MACD_HIST"]) else None,
            "ema20":     round(float(last["EMA20"]), 4)     if pd.notna(last["EMA20"]) else None,
            "ema50":     round(float(last["EMA50"]), 4)     if pd.notna(last["EMA50"]) else None,
            "bb_up":     round(float(last["BB_UP"]), 4)     if pd.notna(last["BB_UP"]) else None,
            "bb_mb":     round(float(last["BB_MB"]), 4)     if pd.notna(last["BB_MB"]) else None,
            "bb_dn":     round(float(last["BB_DN"]), 4)     if pd.notna(last["BB_DN"]) else None,
            "bb_width":  round(float(last["BB_WIDTH"]), 4)  if pd.notna(last["BB_WIDTH"]) else None,
            "obv_trend": "ALCISTA" if (pd.notna(last.get("OBV_MA20")) and last["OBV"] > last["OBV_MA20"])
                         else "BAJISTA" if pd.notna(last.get("OBV_MA20")) else "—",
            "vwap":      round(float(last["VWAP"]), 4) if pd.notna(last.get("VWAP")) else None,
            "levels":    levels,
            "pivots":    pivots,
            "close_long":  close_long,
            "close_short": close_short,
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
    print("Corriendo analisis v2.1...")
    results = run_analysis()
    for r in results:
        if r["error"]:
            print(f"\nERROR {r['name']}: {r['error']}")
        else:
            print(f"\n{'='*60}")
            print(f"{r['name']} | ${r['price']} | Score: {r['score']}/{r['max_score']}")
            print(f"Señal: {r['signal']} | Confianza: {r['confidence']}")
            if r["signal_reason"]:
                print(f"Motivo: {r['signal_reason']}")
            print(f"ADX: {r['adx']} | Trending: {r['market_trending']}")
            print(f"Tendencias: 1H={r['trend_1h']} | 1D={r['trend_1d']} | 1W={r['trend_1w']}")
            print(f"Confirma 1H: {r['confirm_1h']}")
            print(f"Divergencia: alcista={r['divergence']['bullish']} | bajista={r['divergence']['bearish']}")
            print(f"OBV: {r['obv_trend']} | BB Squeeze: {r['bb_squeeze']}")
            if r["warnings"]:
                for w in r["warnings"]:
                    print(f"  AVISO: {w}")
            if r["close_short"]["should_close"]:
                print(f"  CERRAR SHORT ({r['close_short']['urgency']}): {r['close_short']['reasons']}")
            if r["close_long"]["should_close"]:
                print(f"  CERRAR LONG ({r['close_long']['urgency']}): {r['close_long']['reasons']}")
